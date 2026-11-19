"""`pixamind integrity`: projection fingerprints, and where two databases differ.

    pixamind integrity --database-url URL                      fingerprints of one database
    pixamind integrity --database-url A --compare-url B        compare A with B (exit 1 on difference)
        [--projections posts,votes,...] [--buckets 256] [--show 10] [--json] [--force]

        [--install-functions]

Use it to check that two databases built from the same blocks agree: two API
nodes, a replay after a change, or an original Hivemind database against a
Pixamind one. A Hivemind database has no pixamind schema: --install-functions
adds just the fingerprint functions to any database that lacks them (it needs
CREATE rights there), and the hivemind projections can then be compared.

Stop both indexers at the same block first (`--test-max-block`): a database
that has processed more blocks legitimately differs. For each differing
projection the tool narrows the difference down by hash buckets and prints
sample rows that exist on one side only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg2

FINGERPRINT_SQL = Path(__file__).resolve().parent.parent / 'sql' / 'runtime' / '70_fingerprints.sql'
HIVEMIND_PROJECTIONS = [
    'accounts',
    'posts',
    'post_data',
    'post_tags',
    'votes',
    'follows',
    'muted',
    'blacklisted',
    'follow_muted',
    'follow_blacklisted',
    'reblogs',
    'feed_cache',
    'communities',
    'roles',
    'subscriptions',
    'mentions',
    'notifications',
]


def connect(url: str):
    conn = psycopg2.connect(url, application_name='pixamind_integrity')
    conn.autocommit = True
    return conn


def query(conn, sql: str, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args or None)
        return cur.fetchall() if cur.description else []


def processed_block(conn) -> int | None:
    try:
        return query(conn, "SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app'")[0][0]
    except (psycopg2.Error, IndexError):
        return None


def has_fingerprints(conn) -> bool:
    return bool(query(conn, "SELECT to_regprocedure('pixamind.projection_fingerprints(text[])') IS NOT NULL")[0][0])


def has_pixamind_tables(conn) -> bool:
    return bool(query(conn, "SELECT to_regclass('pixamind.events') IS NOT NULL")[0][0])


def install_functions(conn) -> None:
    """Fingerprint functions only (no tables), e.g. on an original Hivemind database."""
    with conn.cursor() as cur:
        cur.execute('CREATE SCHEMA IF NOT EXISTS pixamind')
        cur.execute(FINGERPRINT_SQL.read_text())


def fingerprints(conn, projections: list[str] | None) -> dict[str, tuple[int, str]]:
    rows = query(conn, 'SELECT projection, rows, fingerprint FROM pixamind.projection_fingerprints(%s)', projections)
    return {name: (count, digest) for name, count, digest in rows}


def bucket_diff(left, right, projection: str, buckets: int) -> list[int]:
    sql = 'SELECT bucket, rows, fingerprint FROM pixamind.projection_bucket_fingerprints(%s, %s)'
    a = {b: (n, f) for b, n, f in query(left, sql, projection, buckets)}
    b = {b: (n, f) for b, n, f in query(right, sql, projection, buckets)}
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def row_diff(left, right, projection: str, bucket: int, buckets: int, limit: int) -> tuple[list[str], list[str]]:
    sql = 'SELECT * FROM pixamind.projection_rows(%s, %s, %s, %s)'
    a = {r[0] for r in query(left, sql, projection, bucket, buckets, 100000)}
    b = {r[0] for r in query(right, sql, projection, bucket, buckets, 100000)}
    return sorted(a - b)[:limit], sorted(b - a)[:limit]


def compare(left, right, projections, buckets: int, show: int) -> dict:
    fl, fr = fingerprints(left, projections), fingerprints(right, projections)
    report = {'projections': {}, 'equal': True}
    for name in sorted(set(fl) | set(fr)):
        entry = {'left': fl.get(name), 'right': fr.get(name), 'equal': fl.get(name) == fr.get(name)}
        if not entry['equal']:
            report['equal'] = False
            differing = bucket_diff(left, right, name, buckets)
            entry['differing_buckets'] = len(differing)
            samples = []
            for bucket in differing:
                only_left, only_right = row_diff(left, right, name, bucket, buckets, show)
                samples += [{'side': 'left', 'row': r} for r in only_left] + [
                    {'side': 'right', 'row': r} for r in only_right
                ]
                if len(samples) >= show:
                    break
            entry['samples'] = samples[:show]
        report['projections'][name] = entry
    return report


def render(report: dict) -> str:
    lines = [f"{'projection':<20} {'left rows':>10} {'right rows':>10}  result"]
    for name, entry in report['projections'].items():
        left = entry['left'][0] if entry['left'] else '-'
        right = entry['right'][0] if entry['right'] else '-'
        verdict = 'ok' if entry['equal'] else f"DIFFERENT ({entry.get('differing_buckets', '?')} buckets)"
        lines.append(f'{name:<20} {left:>10} {right:>10}  {verdict}')
        for sample in entry.get('samples', []):
            lines.append(f"    only {sample['side']:<5} {sample['row'][:200]}")
    lines.append('EQUAL' if report['equal'] else 'DIFFERENT')
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind integrity', description=__doc__.splitlines()[0])
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--compare-url', help='second database to compare with')
    parser.add_argument('--projections', help='comma-separated subset (default: all)')
    parser.add_argument(
        '--buckets', type=int, default=256, help='hash buckets used to locate differences (default 256)'
    )
    parser.add_argument('--show', type=int, default=10, help='sample rows to print per differing projection')
    parser.add_argument(
        '--force', action='store_true', help='compare even when the databases stopped at different blocks'
    )
    parser.add_argument(
        '--install-functions',
        action='store_true',
        help='install the fingerprint functions where missing (e.g. an original Hivemind database)',
    )
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    projections = [p.strip() for p in args.projections.split(',')] if args.projections else None

    left = connect(args.database_url)
    right = connect(args.compare_url) if args.compare_url else None
    for conn in filter(None, (left, right)):
        if not has_fingerprints(conn):
            if not args.install_functions:
                print('a database has no pixamind fingerprint functions; pass --install-functions', file=sys.stderr)
                return 2
            install_functions(conn)
        # a database without Pixamind tables (original Hivemind) only has the hivemind projections
        if projections is None and not has_pixamind_tables(conn):
            projections = HIVEMIND_PROJECTIONS

    if right is None:
        prints = fingerprints(left, projections)
        if args.json:
            print(json.dumps({'processed_block': processed_block(left), 'fingerprints': prints}, indent=2))
        else:
            print(f'processed block {processed_block(left)}')
            for name, (count, digest) in prints.items():
                print(f'{name:<20} {count:>10}  {digest}')
        return 0

    blocks = (processed_block(left), processed_block(right))
    if blocks[0] != blocks[1] and not args.force:
        print(
            f'databases stopped at different blocks {blocks[0]} and {blocks[1]}; '
            'stop both at the same block (--test-max-block) or pass --force',
            file=sys.stderr,
        )
        return 2
    report = compare(left, right, projections, args.buckets, args.show)
    report['processed_blocks'] = blocks
    print(json.dumps(report, indent=2, default=list) if args.json else render(report))
    return 0 if report['equal'] else 1
