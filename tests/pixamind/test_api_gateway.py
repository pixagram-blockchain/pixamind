"""pixamind-api, the Rust server that replaces PostgREST and the nginx rewriter.

The binary runs against a synced test database. Its answers are compared with
the SQL functions it calls, which is what PostgREST returns. With
PIXAMIND_TEST_POSTGREST pointing at a PostgREST binary (CI downloads the
version Pixamind's image uses, 12.0.2), every case is also sent to a real
PostgREST and the two must agree.

Two differences are deliberate. Queries run in read-only transactions, so a
function that writes fails with 405 (25006) where PostgREST would run a
VOLATILE function read-write; the API functions only read. And HEAD returns
the headers the function sets, as GET does; PostgREST leaves them out.

The binary is target/debug/pixamind-api or target/release/pixamind-api
(`cargo build`), or PIXAMIND_API_BIN. Without one the tests are skipped, unless
PIXAMIND_REQUIRE_API_BIN is set (CI).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import harness
import psycopg2
import pytest
import scenario
from livesync import Chain, Indexer

REPO = Path(__file__).resolve().parents[2]
DB = 'pixamind_api_test'
REQUEST_TIMEOUT_S = 2

# Functions that exercise PostgREST's calling conventions, created in the test
# database only.
PROBES = r"""
CREATE FUNCTION hivemind_endpoints.pixamind_test_echo("a-b" TEXT, "n" INT = 5, "flag" BOOLEAN = NULL)
RETURNS JSON LANGUAGE plpgsql STABLE AS $$
BEGIN
  PERFORM set_config('response.headers', '[{"Cache-Control": "public, max-age=7"}, {"X-Echo": "yes"}]', true);
  RETURN json_build_object('a-b', "a-b", 'n', "n", 'flag', "flag");
END $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_status("code" INT)
RETURNS JSON LANGUAGE plpgsql STABLE AS $$
BEGIN
  PERFORM set_config('response.status', "code"::TEXT, true);
  RETURN json_build_object('status', "code");
END $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_raise("code" TEXT, "message" TEXT = 'boom')
RETURNS JSON LANGUAGE plpgsql STABLE AS $$
BEGIN
  RAISE EXCEPTION USING ERRCODE = "code", MESSAGE = "message", DETAIL = 'some detail', HINT = 'some hint';
END $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_rows("n" INT)
RETURNS TABLE (i INT, label TEXT) LANGUAGE sql STABLE AS $$ SELECT g, 'row ' || g FROM generate_series(1, "n") g $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_ints("n" INT)
RETURNS SETOF INT LANGUAGE sql STABLE AS $$ SELECT generate_series(1, "n") $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_sleep("seconds" FLOAT)
RETURNS JSON LANGUAGE sql STABLE AS $$ SELECT json_build_object('slept', pg_sleep("seconds") IS NOT NULL) $$;

CREATE FUNCTION hivemind_endpoints.pixamind_test_write()
RETURNS JSON LANGUAGE plpgsql VOLATILE AS $$ BEGIN CREATE TABLE public.pixamind_test_written (x INT); RETURN '{}'; END $$;
"""

JSON_RPC = [
    ('bridge.get_post', {'author': 'alice', 'permlink': 'cat'}),
    ('bridge.get_ranked_posts', {'sort': 'promoted'}),
    ('bridge.get_ranked_posts', {'sort': 'promoted', 'tag': 'cat'}),
    ('bridge.get_ranked_posts', {'sort': 'promoted', 'observer': 'dave'}),
    ('bridge.get_ranked_posts', {'sort': 'nope'}),
    ('bridge.get_community', {'name': 'portal-100001'}),
    ('bridge.list_subscribers', {'community': 'portal-100001'}),
    ('condenser_api.get_discussions_by_promoted', {'tag': '', 'limit': 10}),
    ('pixamind_api.get_events', {'limit': 1000}),
    ('pixamind_api.get_events', {'types': ['nope']}),
    ('pixamind_content.get_content_metadata', {'author': 'alice', 'permlink': 'cat'}),
    ('pixamind_feed.get_promoted_posts', {'limit': 1}),
    ('pixamind_feed.get_promotions', {'sender': 'dave'}),
    ('pixamind_graph.get_mutual_follows', {'account': 'alice'}),
    ('pixamind_graph.get_relationships', {'observer': 'alice', 'accounts': ['bob', 'carol']}),
    ('no_such_api.method', {}),
]


def rpc_body(method: str, params, request_id=1) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})


# ------------------------------------------------------------------ plumbing


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    text: str

    def json(self):
        return json.loads(self.text)


@dataclass
class Server:
    port: int
    admin_port: int | None = None
    process: subprocess.Popen | None = None
    log: Path | None = None
    extra: dict = field(default_factory=dict)

    def request(
        self, method: str, path: str, body: str | bytes | None = None, headers: dict | None = None, admin=False
    ) -> Reply:
        conn = http.client.HTTPConnection('127.0.0.1', self.admin_port if admin else self.port, timeout=30)
        try:
            all_headers = {'Content-Type': 'application/json'} if body is not None else {}
            all_headers.update(headers or {})
            conn.request(method, path, body=body, headers=all_headers)
            response = conn.getresponse()
            text = response.read().decode()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, text)
        finally:
            conn.close()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_until(check, what: str, server: Server, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            break
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(0.1)
    log = server.log.read_text() if server.log and server.log.exists() else ''
    pytest.fail(f'{what} did not come up:\n{log[-4000:]}')


def api_binary() -> Path | None:
    configured = os.environ.get('PIXAMIND_API_BIN')
    paths = (
        [Path(configured)]
        if configured
        else [REPO / 'target' / 'debug' / 'pixamind-api', REPO / 'target' / 'release' / 'pixamind-api']
    )
    for path in paths:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    if os.environ.get('PIXAMIND_REQUIRE_API_BIN'):
        pytest.fail('pixamind-api binary not found (PIXAMIND_API_BIN)')
    return None


class Db:
    def __init__(self, dsn: str):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def one(self, query: str, *args):
        with self.conn.cursor() as cur:
            cur.execute(query, args or None)
            return cur.fetchone()[0]

    def run(self, query: str, *args) -> None:
        with self.conn.cursor() as cur:
            cur.execute(query, args or None)

    def close(self) -> None:
        self.conn.close()


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope='module')
def database(pg_server):
    binary = api_binary()
    if binary is None:
        pytest.skip('pixamind-api is not built (cargo build), or set PIXAMIND_API_BIN')
    app_dsn = harness.build_database(DB)
    indexer = Indexer(app_dsn)
    scenario.load(Chain(indexer.db))
    for block in range(1, max(scenario.BLOCKS) + 1):
        indexer.process_live(block)
    indexer.close()
    db = Db(harness.test_dsn(DB))
    db.run(PROBES)
    yield app_dsn, db, binary
    db.close()
    admin = psycopg2.connect(harness.ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)')
    admin.close()


@pytest.fixture(scope='module')
def api(database, tmp_path_factory):
    app_dsn, _, binary = database
    server = Server(port=free_port(), admin_port=free_port())
    server.log = tmp_path_factory.mktemp('api') / 'pixamind-api.log'
    command = [
        str(binary),
        '--database-url', app_dsn,
        '--listen', f'127.0.0.1:{server.port}',
        '--admin-listen', f'127.0.0.1:{server.admin_port}',
        '--request-timeout', f'{REQUEST_TIMEOUT_S}s',
        '--pool-size', '4',
        '--log', 'pixamind_api=debug',
    ]  # fmt: skip
    with server.log.open('w') as log:
        server.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    wait_until(lambda: server.request('GET', '/ready', admin=True).status == 200, 'pixamind-api', server)
    yield server
    server.stop()


@pytest.fixture
def db(database):
    return database[1]


# ------------------------------------------------------------------ JSON-RPC


@pytest.mark.parametrize(('method', 'params'), JSON_RPC, ids=[f'{m}-{i}' for i, (m, _) in enumerate(JSON_RPC)])
def test_json_rpc_answers_exactly_what_the_sql_returns(api, db, method, params):
    body = rpc_body(method, params)
    expected = db.one('SELECT hivemind_endpoints.home(%s::json)::text', body)
    for path in ('/', '/api', '/rpc/home'):
        reply = api.request('POST', path, body)
        assert (reply.status, reply.headers['content-type']) == (200, 'application/json; charset=utf-8'), reply
        assert reply.text == expected, path


def test_json_rpc_quirks_pass_through(api, db):
    # home() answers these itself (string id, error strings): the server mustn't "fix" them.
    for body in (
        '{"jsonrpc": "2.0", "method": "bridge.get_post"}',
        '[{"jsonrpc": "2.0", "id": 1}]',
        '{"jsonrpc": "2.0", "id": 7, "method": "bridge.get_post"}',
    ):
        reply = api.request('POST', '/', body)
        assert reply.status == 200
        assert reply.text == db.one('SELECT hivemind_endpoints.home(%s::json)::text', body)
    assert (
        api.request('POST', '/', rpc_body('bridge.get_post', {'author': 'alice', 'permlink': 'cat'}, 42)).json()['id']
        == '42'
    )


def test_invalid_json_is_rejected_like_postgrest(api):
    reply = api.request('POST', '/', '{nope')
    assert reply.status == 400
    assert reply.json() == {'code': 'PGRST102', 'details': None, 'hint': None, 'message': 'Empty or invalid json'}


def test_get_on_the_root_returns_the_openapi_document(api, db):
    reply = api.request('GET', '/')
    assert reply.status == 200
    assert reply.json() == json.loads(db.one('SELECT hivemind_endpoints.home()::text'))
    assert reply.json()['openapi'].startswith('3.')


# ------------------------------------------------------------------ REST


def test_rest_paths_follow_the_rewrite_rules(api, db):
    reply = api.request('GET', '/sync-status')
    assert reply.status == 200
    assert reply.headers['cache-control'] == 'public, max-age=0'
    assert reply.json() == json.loads(db.one('SELECT hivemind_endpoints.get_sync_status()::text'))

    reply = api.request('GET', '/blog/reblogs?sort=created&observer=alice')
    assert reply.status == 200
    assert reply.text == db.one(
        """SELECT coalesce(json_agg(r)::text, '[]') FROM hivemind_endpoints.get_reblogs("sort" => 'created', "observer" => 'alice') r"""
    )
    # PostgREST's own path, with the arguments in a JSON body (how the tavern suite calls it)
    same = api.request('POST', '/rpc/get_reblogs', json.dumps({'sort': 'created', 'observer': 'alice'}))
    assert (same.status, same.text) == (200, reply.text)


def test_rest_errors_are_postgrest_shaped(api):
    reply = api.request('GET', '/blog/reblogs?sort=bogus&observer=alice')
    assert reply.status == 400
    assert reply.json() == {
        'code': 'P0001',
        'details': None,
        'hint': None,
        'message': 'Unsupported sort, valid sorts: trending, hot, created, payout, payout_comments, muted',
    }


# ------------------------------------------------------------------ PostgREST conventions


def test_arguments_are_named_typed_and_defaulted(api):
    reply = api.request('GET', '/rpc/pixamind_test_echo?a-b=x')
    assert reply.status == 200
    assert reply.json() == {'a-b': 'x', 'n': 5, 'flag': None}
    assert reply.headers['cache-control'] == 'public, max-age=7'
    assert reply.headers['x-echo'] == 'yes'
    assert api.request('GET', '/rpc/pixamind_test_echo?a-b=x%20y&n=7&flag=true').json() == {
        'a-b': 'x y',
        'n': 7,
        'flag': True,
    }
    posted = api.request('POST', '/rpc/pixamind_test_echo', json.dumps({'a-b': 'x', 'n': 7}))
    assert posted.json() == {'a-b': 'x', 'n': 7, 'flag': None}


def test_wrong_arguments(api):
    reply = api.request('GET', '/rpc/pixamind_test_echo?a-b=x&n=seven')
    assert (reply.status, reply.json()['code']) == (400, '22P02')
    for path in ('/rpc/pixamind_test_echo?bogus=1', '/rpc/pixamind_test_echo?n=1', '/rpc/no_such_function'):
        reply = api.request('GET', path)
        assert (reply.status, reply.json()['code']) == (404, 'PGRST202'), path
    hint = api.request('GET', '/rpc/pixamind_test_echo?bogus=1').json()['hint']
    assert hint == 'Perhaps you meant to call the function hivemind_endpoints.pixamind_test_echo(a-b, flag, n)'


@pytest.mark.parametrize(
    ('code', 'status'),
    [('P0001', 400), ('PT402', 402), ('22012', 400), ('55000', 500), ('42501', 401), ('23505', 409), ('P0002', 500)],
)
def test_sql_errors_become_http_statuses(api, code, status):
    reply = api.request('GET', f'/rpc/pixamind_test_raise?code={code}&message=nope')
    assert reply.status == status
    assert reply.json() == {'code': code, 'details': 'some detail', 'hint': 'some hint', 'message': 'nope'}
    assert reply.headers['content-type'] == 'application/json; charset=utf-8'
    # PostgREST answers an anonymous permission error with 401 and a Bearer challenge
    assert reply.headers.get('www-authenticate') == ('Bearer' if status == 401 else None)
    assert 'content-range' not in reply.headers


def test_functions_can_set_the_status(api):
    assert api.request('GET', '/rpc/pixamind_test_status?code=201').status == 201


def test_sets_are_arrays(api, db):
    rows = api.request('GET', '/rpc/pixamind_test_rows?n=2')
    assert rows.text == db.one('SELECT json_agg(t)::text FROM hivemind_endpoints.pixamind_test_rows(2) t')
    assert rows.json() == [{'i': 1, 'label': 'row 1'}, {'i': 2, 'label': 'row 2'}]
    assert rows.headers['content-range'] == '0-1/*'
    empty = api.request('GET', '/rpc/pixamind_test_rows?n=0')
    assert (empty.text, empty.headers['content-range']) == ('[]', '*/*')
    assert api.request('GET', '/rpc/pixamind_test_ints?n=3').json() == [1, 2, 3]
    assert api.request('GET', '/sync-status').headers['content-range'] == '0-0/*'


def test_queries_run_read_only(api, db):
    reply = api.request('POST', '/rpc/pixamind_test_write', '{}')
    assert (reply.status, reply.json()['code']) == (405, '25006')
    assert db.one("SELECT to_regclass('public.pixamind_test_written') IS NULL")


def test_slow_requests_are_cancelled(api, db):
    started = time.monotonic()
    reply = api.request('GET', '/rpc/pixamind_test_sleep?seconds=30')
    assert reply.status == 504
    assert time.monotonic() - started < REQUEST_TIMEOUT_S + 3
    deadline = time.monotonic() + 5
    while db.one(
        "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%%pixamind_test_sleep%%' AND state = 'active' AND pid <> pg_backend_pid()"
    ):
        assert time.monotonic() < deadline, 'the query kept running'
        time.sleep(0.1)


def test_new_functions_are_found_without_a_restart(api, db):
    # A miss re-reads the catalog, at most every 2 seconds.
    db.run(
        'CREATE FUNCTION hivemind_endpoints.pixamind_test_late() RETURNS JSON LANGUAGE sql STABLE AS $$ SELECT \'"late"\'::json $$'
    )
    deadline = time.monotonic() + 5
    while (reply := api.request('GET', '/rpc/pixamind_test_late')).status == 404 and time.monotonic() < deadline:
        time.sleep(0.25)
    assert (reply.status, reply.json()) == (200, 'late')


# ------------------------------------------------------------------ HTTP


def test_methods_and_cors(api):
    put = api.request('PUT', '/rpc/pixamind_test_echo?a-b=x')
    assert (put.status, put.json()['message']) == (405, 'Cannot use the PUT method on RPC')
    preflight_headers = {
        'Origin': 'https://pixagram.com',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type',
    }
    preflight = api.request('OPTIONS', '/', headers=preflight_headers)
    assert preflight.status == 200
    assert preflight.headers['access-control-allow-origin'] == '*'
    assert preflight.headers['access-control-allow-methods'] == 'GET, POST, PATCH, PUT, DELETE, OPTIONS, HEAD'
    assert (
        preflight.headers['access-control-allow-headers']
        == 'Authorization, content-type, Accept, Accept-Language, Content-Language'
    )
    assert preflight.headers['access-control-max-age'] == '86400'
    body = rpc_body('bridge.get_post', {'author': 'alice', 'permlink': 'cat'})
    simple = api.request('POST', '/', body, headers={'Origin': 'https://pixagram.com'})
    assert simple.headers['access-control-allow-origin'] == '*'
    assert 'Content-Range' in simple.headers['access-control-expose-headers']
    assert 'access-control-allow-origin' not in api.request('GET', '/sync-status').headers


def test_large_bodies_are_refused(api):
    reply = api.request('POST', '/', '{"x": "' + 'a' * (1024 * 1024 + 10) + '"}')
    assert reply.status == 413


def test_head_requests_have_no_body(api):
    reply = api.request('HEAD', '/sync-status')
    assert (reply.status, reply.text) == (200, '')


def test_admin_endpoints(api, database):
    binary = database[2]
    probe = subprocess.run([str(binary), '--healthcheck', '--admin-listen', f'127.0.0.1:{api.admin_port}'], timeout=10)
    assert probe.returncode == 0  # what the image's HEALTHCHECK runs
    assert api.request('GET', '/health', admin=True).status == 204
    assert api.request('GET', '/live', admin=True).status == 200
    assert api.request('GET', '/ready', admin=True).status == 200
    api.request('POST', '/', rpc_body('bridge.get_post', {'author': 'alice', 'permlink': 'cat'}))
    metrics = api.request('GET', '/metrics', admin=True).text
    assert 'pixamind_api_requests_total{function="home",method="bridge.get_post",status="200"}' in metrics
    assert 'pixamind_api_db_connections{state="size"}' in metrics


def test_connections_are_not_named_like_the_indexer(api, db):
    # the indexer terminates hivemind_* backends when it restarts
    names = db.one(
        "SELECT array_agg(DISTINCT application_name) FROM pg_stat_activity WHERE usename = 'hivemind' AND datname = %s",
        DB,
    )
    assert 'pixamind-api' in names
    assert not any(n.startswith('hivemind_') for n in names)


# ------------------------------------------------------------------ against PostgREST


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str  # through pixamind-api, public path
    postgrest_path: str  # what nginx sent to PostgREST
    body: str | None = None
    exact: bool = True  # byte-identical body, not only equal JSON
    headers: tuple = ()


ORIGIN = (('Origin', 'https://pixagram.com'),)
PREFLIGHT = (*ORIGIN, ('Access-Control-Request-Method', 'POST'), ('Access-Control-Request-Headers', 'content-type'))

PARITY = [
    # nginx sent every JSON-RPC request to /rpc/home
    *(Case(f'rpc-{i}-{m}', 'POST', '/', '/rpc/home', rpc_body(m, p)) for i, (m, p) in enumerate(JSON_RPC)),
    Case(
        'rpc-home-path',
        'POST',
        '/api',
        '/rpc/home',
        rpc_body('bridge.get_post', {'author': 'alice', 'permlink': 'cat'}),
    ),
    Case('rpc-invalid-json', 'POST', '/', '/rpc/home', '{nope'),
    Case('rpc-empty-body', 'POST', '/', '/rpc/home', '', exact=False),
    Case('openapi', 'GET', '/', '/', exact=False),
    Case('sync-status', 'GET', '/sync-status', '/rpc/get_sync_status'),
    Case('reblogs', 'GET', '/blog/reblogs?sort=created&observer=alice', '/rpc/get_reblogs?sort=created&observer=alice'),
    Case(
        'reblogs-error', 'GET', '/blog/reblogs?sort=bogus&observer=alice', '/rpc/get_reblogs?sort=bogus&observer=alice'
    ),
    Case('reblogs-post', 'POST', '/rpc/get_reblogs', '/rpc/get_reblogs', '{"sort": "created", "observer": "alice"}'),
    Case(
        'pending-rewards',
        'GET',
        '/accounts/alice/pending-author-rewards',
        '/rpc/get_account_pending_author_rewards?account-name=alice',
    ),
    Case('echo-defaults', 'GET', '/rpc/pixamind_test_echo?a-b=x', '/rpc/pixamind_test_echo?a-b=x'),
    Case(
        'echo-typed',
        'GET',
        '/rpc/pixamind_test_echo?a-b=x&n=7&flag=true',
        '/rpc/pixamind_test_echo?a-b=x&n=7&flag=true',
    ),
    Case('echo-post', 'POST', '/rpc/pixamind_test_echo', '/rpc/pixamind_test_echo', '{"a-b": "x", "n": 7}'),
    Case('echo-bad-int', 'GET', '/rpc/pixamind_test_echo?a-b=x&n=seven', '/rpc/pixamind_test_echo?a-b=x&n=seven'),
    Case('echo-unknown-arg', 'GET', '/rpc/pixamind_test_echo?bogus=1', '/rpc/pixamind_test_echo?bogus=1', exact=False),
    Case('unknown-function', 'GET', '/rpc/no_such_function', '/rpc/no_such_function', exact=False),
    Case('raise-p0001', 'GET', '/rpc/pixamind_test_raise?code=P0001', '/rpc/pixamind_test_raise?code=P0001'),
    Case('raise-pt402', 'GET', '/rpc/pixamind_test_raise?code=PT402', '/rpc/pixamind_test_raise?code=PT402'),
    Case('raise-55000', 'GET', '/rpc/pixamind_test_raise?code=55000', '/rpc/pixamind_test_raise?code=55000'),
    Case('raise-42501', 'GET', '/rpc/pixamind_test_raise?code=42501', '/rpc/pixamind_test_raise?code=42501'),
    Case('status-201', 'GET', '/rpc/pixamind_test_status?code=201', '/rpc/pixamind_test_status?code=201'),
    Case('rows', 'GET', '/rpc/pixamind_test_rows?n=2', '/rpc/pixamind_test_rows?n=2'),
    Case('rows-empty', 'GET', '/rpc/pixamind_test_rows?n=0', '/rpc/pixamind_test_rows?n=0'),
    Case('ints', 'GET', '/rpc/pixamind_test_ints?n=3', '/rpc/pixamind_test_ints?n=3'),
    Case('put', 'PUT', '/rpc/pixamind_test_echo?a-b=x', '/rpc/pixamind_test_echo?a-b=x', '{}'),
    Case('delete', 'DELETE', '/', '/'),
    Case('cors', 'GET', '/rpc/pixamind_test_echo?a-b=x', '/rpc/pixamind_test_echo?a-b=x', headers=ORIGIN),
    Case('cors-preflight', 'OPTIONS', '/', '/rpc/home', headers=PREFLIGHT),
]
COMPARED_HEADERS = (
    'content-type',
    'content-range',
    'cache-control',
    'x-echo',
    'www-authenticate',
    'access-control-allow-origin',
    'access-control-allow-methods',
    'access-control-allow-headers',
    'access-control-expose-headers',
    'access-control-max-age',
)


@pytest.fixture(scope='module')
def postgrest(database, tmp_path_factory):
    binary = os.environ.get('PIXAMIND_TEST_POSTGREST')
    if not binary:
        pytest.skip('set PIXAMIND_TEST_POSTGREST to a PostgREST binary to compare with it')
    app_dsn = database[0]
    server = Server(port=free_port(), admin_port=free_port())
    server.log = tmp_path_factory.mktemp('postgrest') / 'postgrest.log'
    # The settings of scripts/start_postgrest.sh and the Dockerfile.
    env = dict(
        os.environ,
        PGRST_DB_URI=app_dsn,
        PGRST_DB_SCHEMAS='hivemind_endpoints',
        PGRST_DB_ANON_ROLE='hivemind',
        PGRST_DB_ROOT_SPEC='home',
        PGRST_DB_POOL='4',
        PGRST_SERVER_HOST='127.0.0.1',
        PGRST_SERVER_PORT=str(server.port),
        PGRST_ADMIN_SERVER_PORT=str(server.admin_port),
        PGRST_LOG_LEVEL='info',
    )
    with server.log.open('w') as log:
        server.process = subprocess.Popen([binary], stdout=log, stderr=subprocess.STDOUT, env=env)
    wait_until(lambda: server.request('GET', '/ready', admin=True).status == 200, 'PostgREST', server)
    yield server
    server.stop()


@pytest.mark.parametrize('case', PARITY, ids=[c.name for c in PARITY])
def test_same_answers_as_postgrest(api, postgrest, case):
    ours = api.request(case.method, case.path, case.body, dict(case.headers))
    theirs = postgrest.request(case.method, case.postgrest_path, case.body, dict(case.headers))
    assert ours.status == theirs.status, (ours, theirs)
    for header in COMPARED_HEADERS:
        assert ours.headers.get(header) == theirs.headers.get(header), (header, ours.headers, theirs.headers)
    if case.exact or not theirs.text:
        assert ours.text == theirs.text
    elif theirs.status < 400:
        assert ours.json() == theirs.json()
    else:
        assert ours.json()['code'] == theirs.json()['code'], (ours.text, theirs.text)
