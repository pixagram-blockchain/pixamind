"""`pixamind apidiff`: replay a request corpus against two API nodes, report differences.

    pixamind apidiff --left URL --right URL --corpus corpus.jsonl
        [--ignore PATH ...] [--concurrency 4] [--show 3] [--report diff.json]

Typical use: the current Hivemind node on the left, a Pixamind node synced to the
same block on the right — every difference is either a deliberate Pixamind
change (documented in docs/pixamind/API.md) or a regression. Responses are
compared as JSON after dropping the JSON-RPC `id` and the values at the ignored
paths. Paths use dots, with `*` for any key or index, e.g.
`error.data.stack.*.context.timestamp` (ignored by default: it is a clock).
Exit status: 0 when every response matches, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pixamind.tools.corpus import read
from pixamind.tools.jsonrpc import RpcClient

DEFAULT_IGNORES = ['error.data.stack.*.context.timestamp']


def _matches(path: list[str], pattern: list[str]) -> bool:
    return len(path) == len(pattern) and all(p == '*' or p == k for k, p in zip(path, pattern, strict=True))


def normalize(value, ignores: list[list[str]], path: list[str] | None = None):
    """Copy of `value` without the JSON-RPC id and the ignored paths."""
    path = path or []
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            sub = [*path, str(key)]
            if not path and key in ('id', 'jsonrpc'):
                continue
            if any(_matches(sub, pattern) for pattern in ignores):
                continue
            out[key] = normalize(item, ignores, sub)
        return out
    if isinstance(value, list):
        return [normalize(item, ignores, [*path, str(i)]) for i, item in enumerate(value)]
    return value


def differences(left, right, path: str = '', limit: int = 20) -> list[str]:
    """Human-readable list of differing paths between two JSON values."""
    found: list[str] = []

    def walk(a, b, at: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b), key=str):
                sub = f'{at}.{key}' if at else str(key)
                if key not in a:
                    found.append(f'{sub}: only right = {json.dumps(b[key])[:120]}')
                elif key not in b:
                    found.append(f'{sub}: only left = {json.dumps(a[key])[:120]}')
                else:
                    walk(a[key], b[key], sub)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                found.append(f'{at or "$"}: {len(a)} items left, {len(b)} right')
            for i, (x, y) in enumerate(zip(a, b, strict=False)):
                walk(x, y, f'{at}[{i}]')
        elif a != b:
            found.append(f'{at or "$"}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}')

    walk(left, right, path)
    return found


def run(left_url: str, right_url: str, entries: list[dict], ignores: list[str], concurrency: int, show: int) -> dict:
    patterns = [p.split('.') for p in ignores]
    local = threading.local()

    def clients():
        if not hasattr(local, 'pair'):
            local.pair = (RpcClient(left_url), RpcClient(right_url))
        return local.pair

    def check(index_entry):
        index, entry = index_entry
        left, right = clients()
        a = left.call(entry['method'], entry.get('params'))
        b = right.call(entry['method'], entry.get('params'))
        body_a = a.body if a.error is None else {'transport_error': a.error}
        body_b = b.body if b.error is None else {'transport_error': b.error}
        norm_a, norm_b = normalize(body_a, patterns), normalize(body_b, patterns)
        same = norm_a == norm_b and a.status == b.status
        result = {
            'index': index,
            'name': entry['name'],
            'method': entry['method'],
            'equal': same,
            'seconds': [round(a.seconds, 4), round(b.seconds, 4)],
        }
        if not same:
            result['params'] = entry.get('params')
            result['status'] = [a.status, b.status]
            result['differences'] = differences(norm_a, norm_b, limit=show) or ['(HTTP status differs)']
        return result

    with ThreadPoolExecutor(max_workers=max(concurrency, 1)) as pool:
        results = list(pool.map(check, enumerate(entries)))
    differing = [r for r in results if not r['equal']]
    by_method: dict[str, list[int]] = {}
    for r in results:
        stats = by_method.setdefault(r['method'], [0, 0])
        stats[0] += 1
        stats[1] += 0 if r['equal'] else 1
    return {
        'left': left_url,
        'right': right_url,
        'requests': len(results),
        'differing': len(differing),
        'methods': {m: {'requests': n, 'differing': d} for m, (n, d) in sorted(by_method.items())},
        'differences': differing,
    }


def render(report: dict, show: int) -> str:
    lines = [f"{report['requests']} requests, {report['differing']} differ ({report['left']} vs {report['right']})", '']
    for method, stats in report['methods'].items():
        mark = '' if stats['differing'] == 0 else f"  <- {stats['differing']} differ"
        lines.append(f"  {method:<55} {stats['requests']:>5}{mark}")
    for diff in report['differences'][:50]:
        lines += ['', f"[{diff['index']}] {diff['method']} {json.dumps(diff.get('params'))[:160]}"]
        lines += [f'    {d}' for d in diff['differences'][:show]]
    if len(report['differences']) > 50:
        lines += ['', f"... {len(report['differences']) - 50} more in the --report file"]
    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pixamind apidiff', description=__doc__.splitlines()[0])
    parser.add_argument('--left', required=True, help='reference node, e.g. the current Hivemind')
    parser.add_argument('--right', required=True, help='node under test, e.g. Pixamind')
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--ignore', action='append', default=[], help='JSON path to ignore (repeatable)')
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--show', type=int, default=3, help='differences shown per request')
    parser.add_argument('--report', type=Path, help='write the full report as JSON')
    args = parser.parse_args(argv)
    report = run(args.left, args.right, read(args.corpus), DEFAULT_IGNORES + args.ignore, args.concurrency, args.show)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))
    print(render(report, args.show))
    return 0 if report['differing'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
