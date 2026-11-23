"""`pixamind history`: post and comment versions from before Pixamind recorded them.

pixamind history backfill --database-url URL [--from-block N] [--posts 500]
    Rebuild pixamind.post_versions from the comment operations in HAF, for every
    live post and comment up to the block the indexer has processed, merging
    edits the way hivemind does (a diff-match-patch patch is applied to the
    previous body). Each post is rebuilt from its creation; versions already
    recorded are kept, so it is idempotent and safe while the indexer runs. Run
    it once on a node that indexed posts before recording their history; with
    --from-block N, only posts with an operation from block N on are rebuilt
    (to repair a range listed in pixamind.projection_failures). Every rebuilt
    post's latest version is compared with what hivemind stores; exit 1 on a
    difference.
"""

from __future__ import annotations

import argparse
import json
import sys

import psycopg2
from diff_match_patch import diff_match_patch

from pixamind.indexer.history import FIELDS, sanitize, version_row

COMMENT, DELETE_COMMENT, INEFFECTIVE_DELETE_COMMENT = 1, 17, 73


def merge_body(old_body: str, new_body_def: str) -> str:
    """hivemind's Posts._merge_post_body (hive/indexer/posts.py), without its cache."""
    try:
        dmp = diff_match_patch()
        patch = dmp.patch_fromText(new_body_def)
        if patch is not None and len(patch):
            new_body, results = dmp.patch_apply(patch, old_body)
            if results and not any(results):
                return old_body  # no hunk matched: hivemind keeps the stored body
            return new_body
        return new_body_def
    except ValueError:
        return new_body_def
    except Exception:  # noqa: BLE001 - hivemind falls back to the text as sent
        return new_body_def


def apply(previous: tuple | None, title: str | None, body: str | None, json_metadata: str | None) -> tuple:
    """The post after a comment operation, as hivemind stores it: a creation takes the
    operation's fields; an edit changes only what it sends non-empty."""
    if previous is None:
        return (sanitize(title or ''), sanitize(body or ''), sanitize(json_metadata or ''))
    return (
        sanitize(title) if title else previous[0],
        sanitize(merge_body(previous[1], body)) if body else previous[1],
        sanitize(json_metadata) if json_metadata else previous[2],
    )


def live_operations(ops: list) -> list:
    """A post's comment operations, without those of an earlier incarnation (a post
    deleted and posted again): in its creation block, only what follows the last
    effective delete_comment. The first one is the creation."""
    created_block = ops[0]['created_block']
    in_created = [op for op in ops if op['block_num'] == created_block]
    later = [op for op in ops if op['block_num'] != created_block]
    if any(op['op_type_id'] == INEFFECTIVE_DELETE_COMMENT for op in in_created):
        deletes = []  # hivemind ignores a delete the chain reported as ineffective
    else:
        deletes = [i for i, op in enumerate(in_created) if op['op_type_id'] == DELETE_COMMENT]
    kept = in_created[deletes[-1] + 1 :] if deletes else in_created
    return [op for op in kept + later if op['op_type_id'] == COMMENT]


def post_rows(post_id: int, ops: list, fields: dict, recorded: set) -> tuple[list[dict], tuple | None]:
    """The version rows of one post from its comment operations (recorded ones are not
    written again), and its final state. Same rules as pixamind/indexer/history.py."""
    rows: list[dict] = []
    state = None
    creation = None  # its row, until the first edit gives it the original text
    for op in live_operations(ops):
        title, body, json_metadata = fields[op['op_id']]
        if state is None:
            state = apply(None, title, body, json_metadata)
            creation = version_row(op['op_id'], post_id, op['block_num'], op['date'], True, (None,) * 3, None)
            if op['op_id'] not in recorded:
                rows.append(creation)
            continue
        if creation is not None:  # the first edit: the creation row gets the original text
            if creation['op_id'] in recorded:  # written before: written again, complete, to fill it
                rows.append({**creation, **dict(zip(FIELDS, state, strict=True))})
            else:  # still among this run's rows
                creation.update(zip(FIELDS, state, strict=True))
            creation = None
        new = apply(state, title, body, json_metadata)
        if op['op_id'] not in recorded:
            rows.append(version_row(op['op_id'], post_id, op['block_num'], op['date'], False, new, state))
        state = new
    return rows, state


TARGETS = """
    CREATE TEMP TABLE _history_targets AS
    SELECT ha.name AS author, hpd.permlink, hp.id AS post_id, hp.block_num_created AS created_block
      FROM hivemind_app.hive_posts hp
      JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
      JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
     WHERE hp.counter_deleted = 0 AND hp.id > 0 AND hp.block_num_created <= %(head)s
"""

ONLY_RECENT = """
    CREATE TEMP TABLE _history_recent AS
    SELECT DISTINCT ho.body_value->>'author' AS author, ho.body_value->>'permlink' AS permlink
      FROM hivemind_app.operations_view ho
     WHERE ho.block_num BETWEEN %(first)s AND %(head)s AND ho.op_type_id = 1;
    DELETE FROM _history_targets t
     WHERE NOT EXISTS (SELECT 1 FROM _history_recent r WHERE r.author = t.author AND r.permlink = t.permlink);
"""

# Every comment, delete and ineffective-delete operation of the targets, by post. The
# date is hivemind's (the previous block's, for regular operations), so versions match
# the posts' created/updated times.
OPERATIONS = """
    SELECT t.post_id, ho.id, ho.block_num, COALESCE(prev.created_at, b.created_at), ho.op_type_id, t.created_block
      FROM hivemind_app.operations_view ho
      JOIN _history_targets t ON t.author = ho.body_value->>'author' AND t.permlink = ho.body_value->>'permlink'
      JOIN hivemind_app.blocks_view b ON b.num = ho.block_num
      LEFT JOIN hivemind_app.blocks_view prev ON prev.num = ho.block_num - 1
     WHERE ho.op_type_id IN (1, 17, 73) AND ho.block_num BETWEEN t.created_block AND %(head)s
     ORDER BY t.post_id, ho.id
"""


def backfill(url: str, first: int, posts_per_transaction: int = 500, out=sys.stdout) -> int:
    conn = psycopg2.connect(url, application_name='pixamind_history_backfill')
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app'")
    head = cur.fetchone()[0] or 0
    cur.execute(TARGETS, {'head': head})
    if first > 1:
        cur.execute(ONLY_RECENT, {'first': first, 'head': head})
    cur.execute('CREATE UNIQUE INDEX ON _history_targets (author, permlink)')
    cur.execute('ANALYZE _history_targets')
    cur.execute('SELECT count(*) FROM _history_targets')
    print(f'{cur.fetchone()[0]} posts and comments to rebuild, up to block {head}', file=out)
    conn.commit()

    operations = conn.cursor(name='pixamind_history_operations', withhold=True)
    operations.itersize = 10000
    operations.execute(OPERATIONS, {'head': head})
    written = posts = mismatches = 0
    group: dict[int, list] = {}
    for post_id, op_id, block_num, date, op_type_id, created_block in operations:
        if post_id not in group and len(group) >= posts_per_transaction:
            w, m = rebuild(cur, group, out)
            conn.commit()
            written, posts, mismatches, group = written + w, posts + len(group), mismatches + m, {}
        group.setdefault(post_id, []).append(
            {
                'op_id': op_id,
                'block_num': block_num,
                'date': date,
                'op_type_id': op_type_id,
                'created_block': created_block,
            }
        )
    if group:
        w, m = rebuild(cur, group, out)
        written, posts, mismatches = written + w, posts + len(group), mismatches + m
    operations.close()
    if first <= 1 and not mismatches:
        cur.execute("SELECT pixamind.set_setting('post_history_backfilled', 'true')")
    conn.commit()
    conn.close()
    print(f'{written} versions written for {posts} posts; {mismatches} differ from hivemind', file=out)
    return 1 if mismatches else 0


def rebuild(cur, group: dict[int, list], out) -> tuple[int, int]:
    """Write the versions of a group of posts; returns (rows written, posts that differ from hivemind)."""
    comment_ops = [op['op_id'] for ops in group.values() for op in ops if op['op_type_id'] == COMMENT]
    cur.execute(
        "SELECT id, body_value->>'title', body_value->>'body', body_value->>'json_metadata' "
        'FROM hivemind_app.operations_view WHERE id = ANY(%s)',
        (comment_ops,),
    )
    fields = {op_id: (title, body, json_metadata) for op_id, title, body, json_metadata in cur.fetchall()}
    cur.execute('SELECT op_id FROM pixamind.post_versions WHERE post_id = ANY(%s)', (list(group),))
    recorded = {row[0] for row in cur.fetchall()}
    rows = []
    for post_id, ops in group.items():
        rows.extend(post_rows(post_id, ops, fields, recorded)[0])
    written = 0
    for at in range(0, len(rows), 200):
        cur.execute('SELECT pixamind.write_post_versions(%s::jsonb)', (json.dumps(rows[at : at + 200]),))
        written += cur.fetchone()[0] or 0
    return written, verify(cur, list(group), out)


def verify(cur, post_ids: list[int], out) -> int:
    """Posts whose latest version differs from what hivemind stores."""
    cur.execute(
        """
        SELECT p.id, ha.name || '/' || hpd.permlink
          FROM unnest(%s::int[]) AS p(id)
          JOIN hivemind_app.hive_posts hp ON hp.id = p.id
          JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
          JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
          JOIN hivemind_app.hive_post_data d ON d.id = p.id
          CROSS JOIN LATERAL (
            SELECT s.title, s.body, s.json_metadata FROM pixamind.post_version_states(p.id) s
             ORDER BY s.version DESC LIMIT 1) last
         WHERE (last.title, last.body, last.json_metadata) IS DISTINCT FROM (d.title, d.body, d.json)
        """,
        (post_ids,),
    )
    rows = cur.fetchall()
    for post_id, key in rows[:10]:
        print(f'differs from hivemind: {key} (post {post_id})', file=out)
    return len(rows)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind history', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backfill', help='rebuild post versions from HAF')
    b.add_argument('--database-url', required=True)
    b.add_argument('--from-block', type=int, default=1, help='only posts with an operation from this block on')
    b.add_argument('--posts', type=int, default=500, help='posts per transaction')
    args = parser.parse_args(argv)
    return backfill(args.database_url, max(args.from_block, 1), max(args.posts, 1))
