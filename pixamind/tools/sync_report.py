"""`pixamind sync-report`: where indexing time goes, from an indexer log.

    pixamind sync-report hivemind_sync.log [--top 10] [--json]

Reads the lines the block processor already writes:
    [PHASE-SUMMARY] blocks=1-1000 total=12.345s load=1.2 accounts_community=0.1 posts=4.5 ...
    [PROCESS LIVE SQL] blocks 1201-1201 in 0.0412s
and reports blocks per second, the share of each phase, and the slowest batches.
This is the baseline step of the proposal's Phase 1: measure before optimising.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pixamind.tools.bench import percentile

MASSIVE = re.compile(r'\[PHASE-SUMMARY\] blocks=(\d+)-(\d+) total=([\d.]+)s(.*)$')
LIVE = re.compile(r'\[PROCESS LIVE SQL\] blocks (\d+)-(\d+) in ([\d.]+)s')
PHASE = re.compile(r'(\w+)=([\d.]+)')


def parse(lines) -> dict:
    batches, live = [], []
    for line in lines:
        if m := MASSIVE.search(line):
            phases = {k: float(v) for k, v in PHASE.findall(m.group(4))}
            batches.append(
                {'first': int(m.group(1)), 'last': int(m.group(2)), 'seconds': float(m.group(3)), 'phases': phases}
            )
        elif m := LIVE.search(line):
            live.append({'first': int(m.group(1)), 'last': int(m.group(2)), 'seconds': float(m.group(3))})
    return {'massive': batches, 'live': live}


def report(parsed: dict, top: int) -> dict:
    out: dict = {}
    batches = parsed['massive']
    if batches:
        blocks = sum(b['last'] - b['first'] + 1 for b in batches)
        seconds = sum(b['seconds'] for b in batches)
        phase_totals: dict[str, float] = {}
        phase_series: dict[str, list[float]] = {}
        for b in batches:
            for k, v in b['phases'].items():
                phase_totals[k] = phase_totals.get(k, 0.0) + v
                phase_series.setdefault(k, []).append(v)
        out['massive'] = {
            'batches': len(batches),
            'blocks': blocks,
            'seconds': round(seconds, 3),
            'blocks_per_second': round(blocks / seconds, 2) if seconds else None,
            'phases': {
                k: {
                    'seconds': round(v, 3),
                    'share': round(v / seconds, 4) if seconds else None,
                    'avg': round(v / len(phase_series[k]), 4),
                    'p95': round(percentile(sorted(phase_series[k]), 95), 4),
                }
                for k, v in sorted(phase_totals.items(), key=lambda kv: -kv[1])
            },
            'slowest': sorted(batches, key=lambda b: -b['seconds'])[:top],
        }
    if parsed['live']:
        lat = sorted(b['seconds'] for b in parsed['live'])
        out['live'] = {
            'blocks': sum(b['last'] - b['first'] + 1 for b in parsed['live']),
            'p50_seconds': percentile(lat, 50),
            'p95_seconds': percentile(lat, 95),
            'p99_seconds': percentile(lat, 99),
            'max_seconds': lat[-1],
            'slowest': sorted(parsed['live'], key=lambda b: -b['seconds'])[:top],
        }
    return out


def render(rep: dict) -> str:
    if not rep:
        return 'no [PHASE-SUMMARY] or [PROCESS LIVE SQL] lines found'
    lines = []
    if 'massive' in rep:
        m = rep['massive']
        lines += [
            f"Massive sync: {m['blocks']} blocks in {m['batches']} batches, {m['seconds']} s, {m['blocks_per_second']} blocks/s",
            '',
            '| phase | seconds | share | avg/batch | p95/batch |',
            '|---|---:|---:|---:|---:|',
        ]
        for name, p in m['phases'].items():
            lines.append(f"| {name} | {p['seconds']} | {p['share'] * 100:.1f}% | {p['avg']} | {p['p95']} |")
        lines += ['', 'Slowest batches:'] + [f"  {b['first']}-{b['last']}: {b['seconds']} s" for b in m['slowest']]
    if 'live' in rep:
        live = rep['live']
        lines += [
            '',
            f"Live sync: {live['blocks']} blocks, per block p50 {live['p50_seconds']} s, p95 {live['p95_seconds']} s, "
            f"p99 {live['p99_seconds']} s, max {live['max_seconds']} s",
        ]
        lines += ['Slowest blocks:'] + [f"  {b['first']}: {b['seconds']} s" for b in live['slowest']]
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind sync-report', description=__doc__.splitlines()[0])
    parser.add_argument('log', type=Path, nargs='?', help='indexer log file (default: stdin)')
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    lines = args.log.read_text(errors='replace').splitlines() if args.log else sys.stdin
    rep = report(parse(lines), args.top)
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    return 0
