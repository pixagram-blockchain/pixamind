"""`pixamind events`: rebuild the event outbox for a block range, or follow it.

pixamind events backfill --database-url URL --from-block A --to-block B [--chunk 1000]
    Re-derive events and promotions of blocks A..B straight from HAF (e.g.
    after enabling the outbox, or to repair a range listed in
    pixamind.projection_failures). Idempotent; safe while the indexer runs,
    for blocks it has already processed.

pixamind events tail --url URL [--from-block N | --cursor C] [--types a,b] [--accounts x,y]
    [--include-reversible] [--interval 3] [--once]
    Print events as JSON lines, polling pixamind_api.get_events.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import psycopg2

from pixamind.tools.jsonrpc import RpcClient


def backfill(url: str, first: int, last: int, chunk: int, out=sys.stdout) -> int:
    conn = psycopg2.connect(url, application_name='pixamind_events_backfill')
    conn.autocommit = False
    processed = conn.cursor()
    processed.execute("SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app'")
    head = processed.fetchone()[0]
    conn.rollback()
    if last > head:
        print(f'blocks above {head} are not processed by the indexer yet; stopping at {head}', file=sys.stderr)
        last = head
    total_events = total_promotions = 0
    start = first
    while start <= last:
        end = min(start + chunk - 1, last)
        with conn.cursor() as cur:
            cur.execute('SELECT pixamind.load_backfill_ops(%s, %s)', (start, end))
            cur.execute("SELECT pixamind.emit_events(%s, %s, 0, 'backfill')", (start, end))
            events = cur.fetchone()[0]
            cur.execute('SELECT pixamind.process_promotions(%s, %s)', (start, end))
            promotions = cur.fetchone()[0]
            cur.execute('SELECT pixamind.emit_promotion_events(%s, %s, 0)', (start, end))
            events += cur.fetchone()[0]
            cur.execute("SELECT pixamind.resolve_failures('events', %s, %s)", (start, end))
            cur.execute("SELECT pixamind.resolve_failures('promotions', %s, %s)", (start, end))
        conn.commit()
        total_events += events
        total_promotions += promotions
        print(f'blocks {start}..{end}: {events} events, {promotions} promotions', file=out)
        start = end + 1
    conn.close()
    print(f'done: {total_events} events, {total_promotions} promotions', file=out)
    return 0


def tail(url: str, params: dict, interval: float, once: bool, out=sys.stdout) -> int:
    client = RpcClient(url)
    try:
        while True:
            page = client.result('pixamind_api.get_events', params)
            for event in page['events']:
                print(json.dumps(event, sort_keys=True), file=out, flush=True)
            if page.get('retention', {}).get('possible_gap'):
                print('warning: events older than the node keeps were pruned past this cursor', file=sys.stderr)
            if page['next_cursor']:
                params = {k: v for k, v in params.items() if k != 'from_block'}
                params['cursor'] = page['next_cursor']
            if once and not page['has_more']:
                return 0
            if not page['has_more']:
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind events', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)

    b = sub.add_parser('backfill', help='rebuild events and promotions for a block range from HAF')
    b.add_argument('--database-url', required=True)
    b.add_argument('--from-block', type=int, required=True)
    b.add_argument('--to-block', type=int, required=True)
    b.add_argument('--chunk', type=int, default=1000)

    t = sub.add_parser('tail', help='print events as JSON lines')
    t.add_argument('--url', required=True)
    t.add_argument('--from-block', type=int)
    t.add_argument('--cursor')
    t.add_argument('--types', help='comma-separated event types')
    t.add_argument('--accounts', help='comma-separated accounts (actor or target)')
    t.add_argument('--include-reversible', action='store_true', help='also blocks that may still be undone by a fork')
    t.add_argument('--interval', type=float, default=3.0, help='seconds between polls once caught up')
    t.add_argument('--limit', type=int, default=500)
    t.add_argument('--once', action='store_true', help='stop when caught up')

    args = parser.parse_args(argv)
    if args.command == 'backfill':
        if args.to_block < args.from_block:
            parser.error('--to-block must not be below --from-block')
        return backfill(args.database_url, args.from_block, args.to_block, args.chunk)

    params: dict = {'limit': args.limit, 'irreversible_only': not args.include_reversible}
    if args.cursor:
        params['cursor'] = args.cursor
    elif args.from_block is not None:
        params['from_block'] = args.from_block
    if args.types:
        params['types'] = [s.strip() for s in args.types.split(',') if s.strip()]
    if args.accounts:
        params['accounts'] = [s.strip() for s in args.accounts.split(',') if s.strip()]
    return tail(args.url, params, args.interval, args.once)
