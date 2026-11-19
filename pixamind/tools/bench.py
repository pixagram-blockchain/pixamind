"""`pixamind bench`: latency percentiles and throughput of an API node for a corpus.

    pixamind bench --url URL --corpus corpus.jsonl [--concurrency 8]
        [--duration 60 | --rounds 3] [--warmup 1] [--json out.json] [--markdown out.md]

Requests are replayed in corpus order by `concurrency` workers, each on its own
keep-alive connection. Latency is measured client-side (network + nginx +
PostgREST + SQL). Compare runs only under identical conditions: same hardware,
PostgreSQL settings, block height, corpus and cache state (see
docs/pixamind/TESTING.md). For browser-like load profiles there is also the k6
suite in tests/performance.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pixamind.tools.corpus import read
from pixamind.tools.jsonrpc import RpcClient


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an ascending list (q in 0..100)."""
    if not sorted_values:
        return None
    rank = max(math.ceil(q / 100 * len(sorted_values)), 1)
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(samples: dict[str, list[tuple[float, bool]]], wall_seconds: float) -> dict:
    methods = {}
    all_lat: list[float] = []
    total = errors = 0
    for method, rows in sorted(samples.items()):
        lat = sorted(s for s, _ in rows)
        errs = sum(1 for _, ok in rows if not ok)
        all_lat += lat
        total += len(rows)
        errors += errs
        methods[method] = {
            'requests': len(rows),
            'errors': errs,
            'p50_ms': _ms(percentile(lat, 50)),
            'p95_ms': _ms(percentile(lat, 95)),
            'p99_ms': _ms(percentile(lat, 99)),
            'max_ms': _ms(lat[-1] if lat else None),
        }
    all_lat.sort()
    return {
        'requests': total,
        'errors': errors,
        'wall_seconds': round(wall_seconds, 3),
        'requests_per_second': round(total / wall_seconds, 2) if wall_seconds > 0 else None,
        'p50_ms': _ms(percentile(all_lat, 50)),
        'p95_ms': _ms(percentile(all_lat, 95)),
        'p99_ms': _ms(percentile(all_lat, 99)),
        'methods': methods,
    }


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 2)


def run(url: str, entries: list[dict], concurrency: int, duration: float | None, rounds: int, warmup: int) -> dict:
    local = threading.local()

    def client() -> RpcClient:
        if not hasattr(local, 'client'):
            local.client = RpcClient(url)
        return local.client

    # warm-up passes are not measured (connection setup, PostgreSQL caches)
    for _ in range(warmup):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda e: client().call(e['method'], e.get('params')), entries))

    samples: dict[str, list[tuple[float, bool]]] = {}
    lock = threading.Lock()
    deadline = None if duration is None else time.perf_counter() + duration
    cursor = {'i': 0}
    limit = None if duration is not None else len(entries) * rounds

    def next_entry():
        with lock:
            if limit is not None and cursor['i'] >= limit:
                return None
            if deadline is not None and time.perf_counter() >= deadline:
                return None
            entry = entries[cursor['i'] % len(entries)]
            cursor['i'] += 1
            return entry

    def worker() -> None:
        while (entry := next_entry()) is not None:
            reply = client().call(entry['method'], entry.get('params'))
            with lock:
                samples.setdefault(entry['method'], []).append((reply.seconds, reply.ok))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in range(concurrency):
            pool.submit(worker)
    report = summarize(samples, time.perf_counter() - start)
    report.update({'url': url, 'concurrency': concurrency, 'corpus_requests': len(entries)})
    return report


def markdown(report: dict) -> str:
    lines = [
        f"**{report['requests']} requests**, {report['errors']} errors, {report['requests_per_second']} req/s, "
        f"concurrency {report['concurrency']} — p50 {report['p50_ms']} ms, p95 {report['p95_ms']} ms, p99 {report['p99_ms']} ms",
        '',
        '| method | requests | errors | p50 ms | p95 ms | p99 ms | max ms |',
        '|---|---:|---:|---:|---:|---:|---:|',
    ]
    for method, m in sorted(report['methods'].items(), key=lambda kv: -(kv[1]['p95_ms'] or 0)):
        lines.append(
            f"| {method} | {m['requests']} | {m['errors']} | {m['p50_ms']} | {m['p95_ms']} | {m['p99_ms']} | {m['max_ms']} |"
        )
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind bench', description=__doc__.splitlines()[0])
    parser.add_argument('--url', required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--duration', type=float, help='seconds to run (default: --rounds passes over the corpus)')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=1, help='unmeasured passes first (default 1)')
    parser.add_argument('--json', type=Path, help='write the report as JSON')
    parser.add_argument('--markdown', type=Path, help='write the report as a Markdown table')
    args = parser.parse_args(argv)
    entries = read(args.corpus)
    if not entries:
        parser.error('the corpus is empty')
    report = run(args.url, entries, max(args.concurrency, 1), args.duration, max(args.rounds, 1), max(args.warmup, 0))
    text = markdown(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    if args.markdown:
        args.markdown.write_text(text + '\n')
    print(text)
    return 0 if report['errors'] == 0 else 1
