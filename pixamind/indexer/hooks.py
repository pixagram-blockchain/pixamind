"""Pixamind's hook into the inherited block processor (hive/indexer/blocks.py).

After hivemind has processed a massive-sync batch, or a live block, the sync
loop calls ``PixamindHooks.after_massive_batch`` / ``after_live_block``. They
run ``pixamind.after_batch`` (promotions, event outbox, telemetry) on the
connection that processed the batch and feed Prometheus.

Guarantees:
* Live: runs inside the block's transaction, under a SAVEPOINT, so Pixamind's
  rows commit atomically with hivemind's and a Pixamind error can never abort
  the block.
* Massive: runs in its own transaction after hivemind's phases; replayed with
  the batch after a crash (every step is idempotent).
* Any failure is logged and counted; the social indexer keeps going.
"""

from __future__ import annotations

import json
import logging
from time import perf_counter

from pixamind.conf import PixamindSettings
from pixamind.observability import metrics

log = logging.getLogger(__name__)

AFTER_BATCH_SIGNATURE = 'pixamind.after_batch(text,integer,integer,real,jsonb,jsonb)'


class PixamindHooks:
    """Process-wide state of the Pixamind hooks (one sync process per database)."""

    enabled = False
    settings = PixamindSettings()
    events_min_block = 0
    _consecutive_errors = 0

    @classmethod
    def configure(cls, conf, db, settings: PixamindSettings | None = None) -> None:
        """Called once when the sync starts, after the schema was installed."""
        if settings is not None:
            cls.settings = settings
        else:
            cls.settings = PixamindSettings.from_conf(conf) if conf is not None else PixamindSettings()
        try:
            from hive.utils.chain import CHAIN
            from pixamind.version import HIVEMIND_BASE, VERSION

            metrics.set_build_info(VERSION, HIVEMIND_BASE, CHAIN)
            cls._warn_if_forking(db)
            installed = db.query_one(f"SELECT to_regprocedure('{AFTER_BATCH_SIGNATURE}') IS NOT NULL")
            if not installed:
                log.warning('Pixamind schema is not installed: promotions, events and telemetry are off for this run')
                cls.enabled = False
                return
            s = cls.settings
            for name, value in (
                ('events', s.events),
                ('events_keep_blocks', s.events_keep_blocks),
                ('promotions', s.promotions),
                ('telemetry', s.telemetry),
            ):
                db.query_no_return('SELECT pixamind.set_setting(:name, :value)', name=name, value=str(value).lower())
            if s.events and s.events_keep_blocks > 0:
                irreversible = db.query_one("SELECT hive.app_get_irreversible_block('hivemind_app')") or 0
                cls.events_min_block = max(irreversible - s.events_keep_blocks, 0)
            else:
                cls.events_min_block = 0
            cls.enabled = s.events or s.promotions or s.telemetry
            log.info(
                'Pixamind hooks: promotions=%s events=%s (from block %s, keep %s blocks) telemetry=%s',
                s.promotions,
                s.events,
                cls.events_min_block,
                s.events_keep_blocks,
                s.telemetry,
            )
        except Exception:
            log.exception('Could not configure the Pixamind hooks; they are off for this run')
            cls.enabled = False

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

    @classmethod
    def options(cls) -> dict:
        s = cls.settings
        return {
            'promotions': s.promotions,
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
