"""Pixamind's hook into the inherited block processor (hive/indexer/blocks.py).

After hivemind has processed a massive-sync batch, or a live block, the sync
loop calls ``PixamindHooks.after_massive_batch`` / ``after_live_block``. They
run ``pixamind.after_batch`` (promotions, event outbox, telemetry) on the
connection that processed the batch and feed Prometheus. Once a live block has
committed, ``after_live_commit`` refreshes the shared feeds cache
(docs/pixamind/FEED-CACHE.md).

Guarantees:
* Live: runs inside the block's transaction, under a SAVEPOINT, so Pixamind's
  rows commit atomically with hivemind's and a Pixamind error can never abort
  the block.
* Massive: runs in its own transaction after hivemind's phases; replayed with
  the batch after a crash (every step is idempotent).
* Shared feeds cache: refreshed after the block's COMMIT, in its own
  transaction, so it never delays the block; rows are tagged with the block
  they were computed at and the API ignores rows of any other block.
* Any failure is logged and counted; the social indexer keeps going.
"""

from __future__ import annotations

import json
import logging
from time import perf_counter

from pixamind.conf import PixamindSettings
from pixamind.indexer.history import PostHistory
from pixamind.observability import metrics

log = logging.getLogger(__name__)

AFTER_BATCH_SIGNATURE = 'pixamind.after_batch(text,integer,integer,real,jsonb,jsonb)'
POST_HISTORY_SIGNATURE = 'pixamind.write_post_versions(jsonb)'
FEED_CACHE_SIGNATURE = 'pixamind.refresh_feed_cache(integer)'


class PixamindHooks:
    """Process-wide state of the Pixamind hooks (one sync process per database)."""

    enabled = False
    feed_cache = False
    settings = PixamindSettings()
    events_min_block = 0
    _consecutive_errors = 0
    _feed_cache_errors = 0

    @classmethod
    def configure(cls, conf, db, settings: PixamindSettings | None = None) -> None:
        """Called once when the sync starts, after the schema was installed."""
        if settings is not None:
            cls.settings = settings
        else:
            cls.settings = PixamindSettings.from_conf(conf) if conf is not None else PixamindSettings()
        cls.feed_cache = False
        PostHistory.enabled = False
        try:
            from hive.utils.chain import CHAIN
            from pixamind.version import HIVEMIND_BASE, VERSION

            metrics.set_build_info(VERSION, HIVEMIND_BASE, CHAIN)
            cls._warn_if_forking(db)
            cls._warn_if_jit(db)
            installed = db.query_one(f"SELECT to_regprocedure('{AFTER_BATCH_SIGNATURE}') IS NOT NULL")
            if not installed:
                log.warning(
                    'Pixamind schema is not installed: promotions, events, telemetry and the feed cache '
                    'are off for this run'
                )
                cls.enabled = False
                return
            s = cls.settings
            feed_cache = cls._reset_feed_cache(db, s.feed_cache)
            post_history = s.post_history and bool(
                db.query_one(f"SELECT to_regprocedure('{POST_HISTORY_SIGNATURE}') IS NOT NULL")
            )
            for name, value in (
                ('events', s.events),
                ('events_keep_blocks', s.events_keep_blocks),
                ('promotions', s.promotions),
                ('nfts', s.nfts),
                ('telemetry', s.telemetry),
                ('feed_cache', feed_cache),
                ('post_history', post_history),
            ):
                db.query_no_return('SELECT pixamind.set_setting(:name, :value)', name=name, value=str(value).lower())
            if s.events and s.events_keep_blocks > 0:
                irreversible = db.query_one("SELECT hive.app_get_irreversible_block('hivemind_app')") or 0
                cls.events_min_block = max(irreversible - s.events_keep_blocks, 0)
            else:
                cls.events_min_block = 0
            cls.enabled = s.events or s.promotions or s.nfts or s.telemetry
            cls.feed_cache = feed_cache
            PostHistory.enabled = post_history
            log.info(
                'Pixamind hooks: promotions=%s nfts=%s events=%s (from block %s, keep %s blocks) telemetry=%s '
                'feed_cache=%s post_history=%s',
                s.promotions,
                s.nfts,
                s.events,
                cls.events_min_block,
                s.events_keep_blocks,
                s.telemetry,
                feed_cache,
                post_history,
            )
            if (
                post_history
                and db.query_one("SELECT pixamind.get_setting('post_history_backfilled', 'false')") != 'true'
            ):
                log.info(
                    'Post history is recorded from now on; `pixamind history backfill` adds the versions '
                    'from before (docs/pixamind/POST-HISTORY.md)'
                )
        except Exception:
            log.exception('Could not configure the Pixamind hooks; they are off for this run')
            cls.enabled = False
            cls.feed_cache = False
            PostHistory.enabled = False

    @staticmethod
    def _reset_feed_cache(db, wanted: bool) -> bool:
        """Empty the shared feeds cache; returns whether it is on for this run.

        Rows are only ever written by this process's live refreshes: a previous run may
        have ended before a rewind of the context, a resync or edits by hand.
        """
        try:
            if not db.query_one(f"SELECT to_regprocedure('{FEED_CACHE_SIGNATURE}') IS NOT NULL"):
                if wanted:
                    log.warning('The shared feeds cache is not installed: it is off for this run')
                return False
            db.query_no_return('SELECT pixamind.clear_feed_cache()')
        except Exception:
            # rows of an earlier run stay, but match only until the next block is indexed
            log.exception('Could not empty the shared feeds cache: it is off for this run')
            return False
        return wanted

    @staticmethod
    def _warn_if_forking(db) -> None:
        """The context is created non-forking (irreversible blocks only); one set up by the
        earlier forking build stays forking until `setup` runs again."""
        try:
            forking = db.query_one("SELECT hive.app_is_forking('hivemind_app')")
        except Exception:  # informational only
            return
        if forking:
            log.warning(
                'The hivemind_app HAF context is still forking (set up by an earlier build): '
                'run setup once to switch it to non-forking, or resync (docs/pixamind/MIGRATION.md)'
            )

    @staticmethod
    def _warn_if_jit(db) -> None:
        """API calls run with JIT off whatever the settings (every function of
        hivemind_endpoints carries `SET jit = off`); the indexer's own sessions rely on
        the database-level setting written by scripts/install_app.sql."""
        try:
            jit = db.query_one("SELECT current_setting('jit')")
        except Exception:  # informational only
            return
        if str(jit).lower() == 'on':
            log.warning(
                "JIT is on for the indexer's database sessions: as the database owner run "
                "`ALTER DATABASE <database> SET jit = off` (scripts/install_app.sql does it), "
                'see docs/pixamind/OPERATIONS.md'
            )

    @classmethod
    def options(cls) -> dict:
        s = cls.settings
        return {
            'promotions': s.promotions,
            'nfts': s.nfts,
            'events': s.events,
            'events_min_block': cls.events_min_block,
            'events_keep_blocks': s.events_keep_blocks,
            'telemetry': s.telemetry,
        }

    @classmethod
    def _call(cls, db, mode: str, first: int, last: int, seconds: float, phases: dict) -> dict:
        row = db.query_one(
            'SELECT pixamind.after_batch(:mode, :first, :last, :seconds, CAST(:phases AS JSONB), CAST(:options AS JSONB))',
            mode=mode,
            first=first,
            last=last,
            seconds=float(seconds),
            phases=json.dumps({k: round(v, 6) for k, v in (phases or {}).items() if isinstance(v, (int, float))}),
            options=json.dumps(cls.options()),
        )
        return row if isinstance(row, dict) else {}

    @classmethod
    def _report(cls, mode, first, last, seconds, phases, summary) -> None:
        metrics.observe_batch(mode, first, last, seconds, phases, summary)
        if summary.get('failures'):
            log.warning(
                'Pixamind: %s projection step(s) failed for blocks %d-%d (see pixamind.projection_failures)',
                summary['failures'],
                first,
                last,
            )
        cls._consecutive_errors = 0

    @classmethod
    def _error(cls, first, last) -> None:
        cls._consecutive_errors += 1
        metrics.count_failure()
        # full traceback the first time, then one line, to keep a broken node's log readable
        log.warning('Pixamind hook failed for blocks %d-%d', first, last, exc_info=cls._consecutive_errors == 1)

    @classmethod
    def _guarded_call(cls, db, mode: str, first: int, last: int, seconds: float, phases: dict) -> dict | None:
        """Run after_batch in its own transaction, or under a savepoint when the caller's
        transaction is open; on any error undo only Pixamind's work and return None."""
        own_trx = not db.is_trx_active()
        try:
            db.query_no_return('START TRANSACTION' if own_trx else 'SAVEPOINT pixamind_after_batch')
            summary = cls._call(db, mode, first, last, seconds, phases)
            db.query_no_return('COMMIT' if own_trx else 'RELEASE SAVEPOINT pixamind_after_batch')
            return summary
        except Exception:
            try:
                db.query_no_return('ROLLBACK' if own_trx else 'ROLLBACK TO SAVEPOINT pixamind_after_batch')
            except Exception:  # noqa: BLE001 - a dead connection surfaces in the caller's next statement
                pass
            cls._error(first, last)
            return None

    @classmethod
    def after_live_block(cls, db, first: int, last: int, seconds: float) -> None:
        """Inside the live block's open transaction, before its COMMIT: Pixamind's rows
        commit atomically with the block, and an error here never aborts the block."""
        if not cls.enabled:
            metrics.observe_batch('live', first, last, seconds, {}, {})
            return
        summary = cls._guarded_call(db, 'live', first, last, seconds, {})
        if summary is not None:
            cls._report('live', first, last, seconds, {}, summary)

    @classmethod
    def after_live_commit(cls, db, first: int, last: int) -> None:
        """Right after a live block's COMMIT: recompute the shared feeds cache for the new
        block, in a transaction of its own. The block is already visible, so the cache
        never delays it; until this commits the API computes those feeds itself (rows of
        the previous block are no longer served). Never raises."""
        if not cls.feed_cache or db.is_trx_active():
            return
        t0 = perf_counter()
        try:
            db.query_no_return('START TRANSACTION')
            summary = db.query_one('SELECT pixamind.refresh_feed_cache()')
            db.query_no_return('COMMIT')
        except Exception:
            try:
                db.query_no_return('ROLLBACK')
            except Exception:  # noqa: BLE001 - a dead connection surfaces in the sync loop
                pass
            cls._feed_cache_errors += 1
            metrics.observe_feed_cache(perf_counter() - t0, None)
            log.warning(
                'Pixamind: shared feeds cache not refreshed for blocks %d-%d (the API computes those feeds)',
                first,
                last,
                exc_info=cls._feed_cache_errors == 1,
            )
            return
        cls._feed_cache_errors = 0
        summary = summary if isinstance(summary, dict) else {}
        seconds = perf_counter() - t0
        metrics.observe_feed_cache(seconds, summary)
        log.debug('Pixamind: feed cache for block %s refreshed in %.3fs: %s', summary.get('block'), seconds, summary)

    @classmethod
    def after_massive_batch(cls, db, first: int, last: int, phases: dict) -> float:
        """After hivemind's phases of a massive batch (normally outside any transaction).
        Returns the seconds it took (reported as the batch's `pixamind` phase)."""
        t0 = perf_counter()
        seconds = sum(v for v in (phases or {}).values() if isinstance(v, (int, float)))
        if not cls.enabled:
            metrics.observe_batch('massive', first, last, seconds, phases, {})
            return perf_counter() - t0
        summary = cls._guarded_call(db, 'massive', first, last, seconds, phases)
        if summary is not None:
            cls._report('massive', first, last, seconds, phases, summary)
        return perf_counter() - t0
