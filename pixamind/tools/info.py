"""`pixamind info`: what a running Pixamind API node reports about itself.

pixamind info --url https://api.example.org [--window 10] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from pixamind.tools.jsonrpc import RpcClient


def _fmt(value, suffix='') -> str:
    return '-' if value is None else f'{value}{suffix}'


def _feed_cache_line(cache: dict) -> str:
    if not cache:
        return 'Feed cache   -'
    if cache.get('standby'):
        return f"Feed cache   {'on' if cache.get('enabled') else 'off'}, {_fmt(cache.get('targets'))} targets (standby: not served)"
    return (
        f"Feed cache   {'on' if cache.get('enabled') else 'off'}, {_fmt(cache.get('fresh'))}/{_fmt(cache.get('targets'))} "
        f"targets fresh at block {_fmt(cache.get('block'))}, {_fmt(cache.get('failing'))} failing"
    )


def render(info: dict, metrics: dict) -> str:
    ctx = metrics.get('context', {})
    lag = metrics.get('lag', {})
    thr = metrics.get('throughput', {})
    lat = metrics.get('live_latency_seconds', {})
    events = metrics.get('events', {})
    failures = metrics.get('failures', {})
    chain = info.get('chain', {})
    lines = [
        f"Pixamind {info.get('version', '?')} (hivemind {info.get('hivemind_base', '?')}) on {chain.get('name', '?')} "
        f"[{chain.get('liquid_symbol', '?')}/{chain.get('stable_symbol', '?')}]",
        f"Context      block {_fmt(ctx.get('processed_block'))}, irreversible {_fmt(ctx.get('irreversible_block'))}, "
        f"stage {_fmt(ctx.get('stage'))}, forking {_fmt(ctx.get('forking'))}, age {_fmt(ctx.get('processed_block_age_seconds'), ' s')}",
        f"Lag          {_fmt(lag.get('blocks_behind_head'))} behind estimated head, "
        f"{_fmt(lag.get('blocks_behind_irreversible'))} behind irreversible, "
        f"{_fmt(lag.get('reversible_blocks_processed'))} reversible processed",
        f"Throughput   {_fmt(thr.get('blocks_per_second'))} blocks/s applied, capacity {_fmt(thr.get('capacity_blocks_per_second'))} "
        f"blocks/s, {_fmt(thr.get('ops_per_second'))} ops/s over {metrics.get('window_minutes', '?')} min",
        f"Block->API   p50 {_fmt(lat.get('p50'), ' s')}, p95 {_fmt(lat.get('p95'), ' s')}, p99 {_fmt(lat.get('p99'), ' s')} "
        f"({_fmt(lat.get('samples'))} live blocks)",
        f"Events       {'on' if events.get('enabled') else 'off'}, blocks {_fmt(events.get('oldest_block'))}.."
        f"{_fmt(events.get('newest_block'))}, ~{_fmt(events.get('estimated_count'))} rows",
        f"Promoted     {_fmt(metrics.get('promoted_posts'))} posts",
        _feed_cache_line(info.get('feed_cache') or {}),
        f"JIT          {_fmt((info.get('database') or {}).get('jit', {}).get('api'))} in API calls, database default "
        f"{_fmt((info.get('database') or {}).get('jit', {}).get('database'))}",
        f"Failures     {_fmt(failures.get('open'))} open",
    ]
    for failure in failures.get('latest', []):
        lines.append(
            f"             {failure.get('projection')} {failure.get('first_block')}..{failure.get('last_block')}: {failure.get('error')}"
        )
    phases = metrics.get('phase_seconds_avg') or {}
    if phases:
        lines.append(
            'Phases (avg s/batch) '
            + ', '.join(f'{k} {v}' for k, v in sorted(phases.items(), key=lambda kv: -float(kv[1])))
        )
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind info', description=__doc__.splitlines()[0])
    parser.add_argument('--url', required=True, help='JSON-RPC endpoint of the node')
    parser.add_argument('--window', type=int, default=10, help='metrics window in minutes (default 10)')
    parser.add_argument('--json', action='store_true', help='print the raw JSON')
    args = parser.parse_args(argv)
    client = RpcClient(args.url)
    try:
        info = client.result('pixamind_api.get_info')
        metrics = client.result('pixamind_api.get_metrics', {'window_minutes': args.window})
    except RuntimeError as ex:
        print(ex, file=sys.stderr)
        return 1
    finally:
        client.close()
    print(json.dumps({'info': info, 'metrics': metrics}, indent=2) if args.json else render(info, metrics))
    return 0
