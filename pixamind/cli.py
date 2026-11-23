"""The ``pixamind`` command.

Indexer modes are the inherited ones (``pixamind sync`` is ``hive sync``):

    pixamind sync | build_schema | upgrade_schema | status   [--database-url ... --pixamind-* ...]

Operator tools:

    pixamind info         node, lag, throughput and latency of a running Pixamind API
    pixamind integrity    projection fingerprints of a database; compare two databases
    pixamind events       backfill the event outbox for a block range, or tail it
    pixamind history      rebuild post and comment versions from HAF (backfill)
    pixamind corpus       build a request corpus (from the API test suite, or sampled from a node)
    pixamind apidiff      replay a corpus against two API nodes and report differences
    pixamind bench        latency percentiles and throughput of an API node for a corpus
    pixamind sync-report  per-phase indexing performance from an indexer log
"""

from __future__ import annotations

import importlib
import sys

TOOLS = {
    'info': 'pixamind.tools.info',
    'integrity': 'pixamind.tools.integrity',
    'events': 'pixamind.tools.events',
    'history': 'pixamind.tools.history',
    'corpus': 'pixamind.tools.corpus',
    'apidiff': 'pixamind.tools.apidiff',
    'bench': 'pixamind.tools.bench',
    'sync-report': 'pixamind.tools.sync_report',
}

INDEXER_MODES = ('sync', 'build_schema', 'upgrade_schema', 'status', 'completion')


def usage() -> str:
    return __doc__.strip() + '\n\nRun `pixamind <tool> --help` or `pixamind sync --help` for options.'


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('-h', '--help', 'help'):
        print(usage())
        return 0
    if argv and argv[0] in ('-V', '--version', 'version'):
        from pixamind.version import HIVEMIND_BASE, VERSION

        print(f'pixamind {VERSION} (hivemind {HIVEMIND_BASE})')
        return 0
    if argv and argv[0] in TOOLS:
        return int(importlib.import_module(TOOLS[argv[0]]).main(argv[1:]) or 0)
    if argv and not argv[0].startswith('-') and argv[0] not in INDEXER_MODES:
        print(f"pixamind: unknown command '{argv[0]}'\n\n{usage()}", file=sys.stderr)
        return 2

    from hive.cli import run

    sys.argv = [sys.argv[0], *argv]
    run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
