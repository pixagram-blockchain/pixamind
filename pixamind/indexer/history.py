"""Every version of every post and comment (pixamind.post_versions).

Hivemind keeps only the latest title, body and json_metadata of a post. Its block
processor (``Blocks._process_post_results_for_cache`` in hive/indexer/blocks.py)
hands each comment operation of a batch to ``PostHistory.add`` right after
merging it (an edit may send a diff-match-patch patch, which hivemind applies
to the stored body), then calls ``PostHistory.flush``. The state after each
operation is written to pixamind.post_versions, keyed by the HAF operation id:

* live sync: inside the block's transaction, under a savepoint, so the versions
  commit with the block and an error here never aborts it;
* massive sync: in a transaction of its own, before hivemind writes the batch's
  post data (the posts' previous state is still readable then); a batch that
  is processed again writes the same rows, which are kept.

Columns that did not change since the post's previous version are stored as
NULL; the first version recorded for a post is complete. A failure is recorded
in pixamind.projection_failures ('post_versions') and the indexer goes on;
`pixamind history backfill` rebuilds missing versions from HAF.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

FIELDS = ('title', 'body', 'json_metadata')


def sanitize(value: str | None) -> str | None:
    """PostgreSQL text cannot hold NUL: hivemind stores a space instead (hive/indexer/post_data_cache.py)."""
    if value is None:
        return None
    return value.replace('\x00', ' ') if '\x00' in value else value


def version_row(
    op_id: int, post_id: int, block_num: int, created_at, is_creation: bool, state: tuple, previous: tuple | None
) -> dict:
    """A pixamind.post_versions row: the full state when there is no previous version,
    otherwise only the fields that changed (NULL for the others)."""
    if previous is None:
        values = state
    else:
        values = tuple(new if new != old else None for new, old in zip(state, previous, strict=True))
    return {
        'op_id': op_id,
        'post_id': post_id,
        'block_num': block_num,
        'created_at': created_at.isoformat() if hasattr(created_at, 'isoformat') else created_at,
        'is_creation': is_creation,
        **dict(zip(FIELDS, values, strict=True)),
    }


def write(db, rows: list[dict], chunk: int = 200) -> int:
    """Insert version rows (bodies can be large: a few hundred per statement)."""
    written = 0
    for start in range(0, len(rows), chunk):
        written += (
            db.query_one(
                'SELECT pixamind.write_post_versions(CAST(:rows AS JSONB))',
                rows=json.dumps(rows[start : start + chunk]),
            )
            or 0
        )
    return written


class PostHistory:
    """Collects the comment operations of a batch, then writes their versions."""

    enabled = False
    _ops: list = []
    _consecutive_errors = 0

    @classmethod
    def add(cls, row: dict, post_data: dict) -> None:
        """One comment operation, as hivemind is about to store it: post_data holds the new
        title/body/json, None for what an edit leaves unchanged (row: post_staging_result)."""
        if cls.enabled:
            cls._ops.append(
                (
                    row['seq_id'],
                    row['post_id'],
                    bool(row['is_new_post']),
                    row['block_num'],
                    row['block_date'],
                    (post_data.get('title'), post_data.get('body'), post_data.get('json')),
                )
            )

    @classmethod
    def flush(cls, db) -> None:
        """Write the versions of the operations added since the last flush. Never raises."""
        ops, cls._ops = cls._ops, []
        if not ops or not cls.enabled:
            return
        first, last = min(op[3] for op in ops), max(op[3] for op in ops)
        own_trx = not db.is_trx_active()
        try:
            db.query_no_return('START TRANSACTION' if own_trx else 'SAVEPOINT pixamind_post_versions')
            write(db, cls.versions(db, ops))
            db.query_no_return('COMMIT' if own_trx else 'RELEASE SAVEPOINT pixamind_post_versions')
            cls._consecutive_errors = 0
        except Exception as error:
            try:
                db.query_no_return('ROLLBACK' if own_trx else 'ROLLBACK TO SAVEPOINT pixamind_post_versions')
            except Exception:  # noqa: BLE001 - a dead connection surfaces in the caller's next statement
                return
            cls._consecutive_errors += 1
            log.warning(
                'Pixamind: post versions of blocks %d-%d not recorded (pixamind history backfill repairs them)',
                first,
                last,
                exc_info=cls._consecutive_errors == 1,
            )
            try:
                db.query_no_return(
                    "SELECT pixamind.record_failure('post_versions', :first, :last, NULL, :error)",
                    first=first,
                    last=last,
                    error=str(error),
                )
            except Exception:  # noqa: BLE001
                log.debug('Could not record the post_versions failure', exc_info=True)

    @staticmethod
    def versions(db, ops: list) -> list[dict]:
        """The rows for a batch's operations, in operation order: a creation is a row
        without text until the post's first edit gives it the original."""
        op_ids = dict(db.query_all('SELECT seq_id, staging_id FROM hivemind_app._comment_staging'))
        created = {op[1] for op in ops if op[2]}
        edited = sorted({op[1] for op in ops if not op[2]} - created)
        state: dict[int, tuple] = {}  # post_id -> (title, body, json_metadata) after its latest version
        recorded: set[int] = set()  # posts with an earlier version in pixamind.post_versions
        no_text: dict[int, dict] = {}  # post_id -> its creation row, still without the original text
        if edited:
            for post_id, title, body, json_metadata, has_versions, marker_op, marker_block, marker_date in db.query_all(
                'SELECT d.id, d.title, d.body, d.json, '
                'EXISTS (SELECT 1 FROM pixamind.post_versions v WHERE v.post_id = d.id), m.op_id, m.block_num, m.created_at '
                'FROM hivemind_app.hive_post_data d '
                'LEFT JOIN pixamind.post_versions m ON m.post_id = d.id AND m.is_creation '
                ' AND num_nulls(m.title, m.body, m.json_metadata) = 3 '
                'WHERE d.id = ANY(:ids)',
                ids=edited,
            ):
                state[post_id] = (title, body, json_metadata)  # as stored before this batch
                if has_versions:
                    recorded.add(post_id)
                if marker_op is not None:
                    no_text[post_id] = version_row(
                        marker_op, post_id, marker_block, marker_date, True, (None,) * 3, None
                    )

        rows = []
        for seq_id, post_id, is_new, block_num, block_date, changes in sorted(ops, key=lambda op: op[0]):
            op_id = op_ids.get(seq_id)
            if op_id is None:
                continue
            changes = tuple(sanitize(value) for value in changes)
            if is_new:
                state[post_id] = tuple(value or '' for value in changes)
                no_text[post_id] = version_row(op_id, post_id, block_num, block_date, True, (None,) * 3, None)
                rows.append(no_text[post_id])
                recorded.add(post_id)
                continue
            before = state.get(post_id)
            if before is None:  # no stored data for the post: nothing to build on
                continue
            if post_id in no_text:  # its first edit: the creation row gets the original text
                creation = no_text.pop(post_id)
                creation.update(zip(FIELDS, before, strict=True))
                if post_id not in created:  # recorded by an earlier batch: written again, complete
                    rows.append(creation)
            new = tuple(before[i] if changes[i] is None else changes[i] for i in range(3))
            rows.append(
                version_row(op_id, post_id, block_num, block_date, False, new, before if post_id in recorded else None)
            )
            state[post_id] = new
            recorded.add(post_id)
        return rows
