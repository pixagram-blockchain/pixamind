"""Fixtures for the Pixamind tests.

Database tests need a PostgreSQL server (any 14+) reachable with a superuser
DSN in PIXAMIND_TEST_ADMIN_DSN, e.g.

    PIXAMIND_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/pixamind

They are skipped when no server answers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402


@pytest.fixture(scope='session')
def pg_server():
    if not harness.server_available():
        pytest.skip('no PostgreSQL server (set PIXAMIND_TEST_ADMIN_DSN)')
    return harness.ADMIN_DSN


@pytest.fixture
def dsn(pg_server):
    """A freshly built test database (real hivemind + Pixamind SQL over stub HAF);
    the DSN logs in as the `hivemind` app role, like the indexer."""
    return harness.build_database()


class Sql:
    """Small psycopg2 helper bound to the test database."""

    def __init__(self, dsn: str):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def one(self, query: str, *args):
        with self.conn.cursor() as cur:
            cur.execute(query, args or None)
            row = cur.fetchone()
            return None if row is None else row[0]

    def all(self, query: str, *args):
        with self.conn.cursor() as cur:
            cur.execute(query, args or None)
            return cur.fetchall()

    def run(self, query: str, *args) -> None:
        with self.conn.cursor() as cur:
            cur.execute(query, args or None)

    def rpc(self, method: str, params=None) -> dict:
        """Call the JSON-RPC endpoint the way PostgREST does; returns the response object."""
        request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
        if params is not None:
            request['params'] = params
        return self.one('SELECT hivemind_endpoints.home(%s::json)', json.dumps(request))

    def result(self, method: str, params=None):
        response = self.rpc(method, params)
        assert 'error' not in response, response
        return response['result']

    def error(self, method: str, params=None) -> dict:
        response = self.rpc(method, params)
        assert 'error' in response, response
        return response['error']

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def sql(dsn):
    helper = Sql(harness.test_dsn())  # superuser: may also edit the stub HAF tables
    yield helper
    helper.close()


@pytest.fixture
def indexer(dsn):
    from livesync import Indexer

    idx = Indexer(dsn)
    yield idx
    idx.close()


@pytest.fixture
def chain(indexer):
    from livesync import Chain

    return Chain(indexer.db)
