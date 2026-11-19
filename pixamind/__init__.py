"""Pixamind: the social indexing and query engine of the Pixa blockchain.

Pixamind is a fork of Hivemind (https://github.com/openhive-network/hivemind).
The inherited engine lives in the ``hive`` package, kept close to upstream so
Hivemind releases can still be merged. Everything Pixamind adds lives here:

    pixamind.conf            command-line/environment options (--pixamind-*)
    pixamind.db              installer for the ``pixamind`` database schema
    pixamind.indexer         per-batch hooks called by the sync loop
    pixamind.observability   Prometheus metrics
    pixamind.tools           operator tools behind the ``pixamind`` command
    pixamind/sql             SQL: migrations (tables) and runtime (functions, API)

See docs/pixamind/ARCHITECTURE.md.
"""

from pixamind.version import HIVEMIND_BASE, VERSION

__all__ = ['HIVEMIND_BASE', 'VERSION']
