"""The shared feeds cache (docs/pixamind/FEED-CACHE.md) and JIT off for API calls.

The scenario is indexed live; after each block's COMMIT the indexer refreshes
pixamind.feed_cache, and hivemind_endpoints.home answers the seeded targets from
it. Every check compares with the same request computed with the cache out of
the way: the bytes must be the same.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg2.errors
import pytest
import scenario
from chain import comment, effective_vote, vote

from pixamind.conf import PixamindSettings
from pixamind.indexer.hooks import PixamindHooks

ROOT = Path(__file__).resolve().parents[2]

# What the Pixagram web app sends (migrations/0002_feed_cache.sql)
TARGETS = [
    ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': '', 'limit': 20}),
    ('bridge.get_ranked_posts', {'sort': 'hot', 'tag': '', 'limit': 20}),
    ('bridge.get_ranked_posts', {'sort': 'promoted', 'tag': '', 'limit': 20}),
    ('bridge.get_ranked_posts', {'sort': 'created', 'tag': '', 'limit': 20}),
    ('condenser_api.get_discussions_by_created', [{'tag': '', 'limit': 20}]),
    ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': '', 'observer': '', 'limit': 6}),
]
TRENDING = TARGETS[0]
CREATED = TARGETS[4]


def body(method, params, request_id=0) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})


def answer(sql, request: str) -> str:
    """The response text, as PostgREST sends it."""
    return sql.one('SELECT hivemind_endpoints.home(%s::json)::text', request)


def computed(sql, request: str) -> str:
    """The same request with the cache out of the way (the deletion is rolled back)."""
    sql.run('BEGIN')
    try:
        sql.run('DELETE FROM pixamind.feed_cache')
        return answer(sql, request)
    finally:
        sql.run('ROLLBACK')


def cached(sql, method, params) -> bool:
    api, name = method.split('.')
    return sql.one(
        'SELECT hivemind_postgrest_utilities.cached_result(%s, %s, %s::jsonb) IS NOT NULL',
        api,
        name,
        json.dumps(params),
    )


def key(method, params) -> tuple[str, str]:
    return method, json.dumps(params, sort_keys=True)


def rows(sql) -> dict:
    """key(method, params) -> (block_num, changed_block, has a result, error)"""
    return {
        key(f'{api}.{method}', params): (block, changed, has_result, error)
        for api, method, params, block, changed, has_result, error in sql.all(
            'SELECT api, method, params, block_num, changed_block, result IS NOT NULL, error FROM pixamind.feed_cache'
        )
    }


@pytest.fixture
def synced(indexer, chain, sql):
    scenario.load(chain)
    for block in range(1, 9):
        indexer.process_live(block)
    return sql


def assert_served_and_identical(sql, targets=TARGETS):
    for method, params in targets:
        request = body(method, params)
        assert cached(sql, method, params), (method, params)
        assert answer(sql, request) == computed(sql, request), (method, params)


# ------------------------------------------------------------------ serving
def test_targets_are_served_byte_identical(synced):
    assert_served_and_identical(synced)
    result = json.loads(answer(synced, body(*CREATED)))['result']
    assert [(p['author'], p['permlink']) for p in result] == [('bob', 'blog-1'), ('alice', 'cat')]


def test_matching_ignores_id_key_order_and_spacing(synced):
    method, params = TRENDING
    reference = computed(synced, body(method, params, 'abc'))
    for request in (
        body(method, params, 'abc'),
        '{"params": {"limit": 20, "tag": "", "sort": "trending"}, "method": "bridge.get_ranked_posts",'
        ' "jsonrpc": "2.0", "id": "abc"}',
        '{"jsonrpc":"2.0","id":"abc","method":"bridge.get_ranked_posts","params":{"sort":"trending","tag":"","limit":20}}',
    ):
        assert answer(synced, request) == reference
    assert json.loads(answer(synced, body(method, params, 7)))['id'] == '7'  # hivemind answers ids as text


def test_other_requests_are_computed(synced):
    for method, params in (
        ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': '', 'limit': 20, 'observer': 'dave'}),
        ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': '', 'limit': 19}),
        ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': 'pixelart', 'limit': 20}),
        ('condenser_api.get_discussions_by_created', {'tag': '', 'limit': 20}),
        ('bridge.get_ranked_posts', {'sort': 'trending', 'tag': '', 'limit': 20.0}),  # equal jsonb, other text
    ):
        assert not cached(synced, method, params)
        assert answer(synced, body(method, params)) == computed(synced, body(method, params))
    # the `call` form reaches dispatch with other params: computed, same answer as the target
    call = json.dumps(
        {
            'jsonrpc': '2.0',
            'id': 0,
            'method': 'call',
            'params': ['condenser_api', 'get_discussions_by_created', [{'tag': '', 'limit': 20}]],
        }
    )
    assert not cached(
        synced,
        'condenser_api.get_discussions_by_created',
        {'used_call_keyword': True, 'params': [{'tag': '', 'limit': 20}]},
    )
    assert json.loads(answer(synced, call))['result'] == json.loads(answer(synced, body(*CREATED)))['result']


def test_api_roles_in_read_only_transactions(synced):
    """As PostgREST runs it: a read-only transaction, the API role set locally."""
    for role in ('hivemind', 'hivemind_user'):
        for method, params in (TRENDING, CREATED):
            request = body(method, params)
            synced.run('BEGIN READ ONLY')
            try:
                synced.run(f'SET LOCAL ROLE {role}')
                text = answer(synced, request)
                served = cached(synced, method, params)
            finally:
                synced.run('ROLLBACK')
            assert served and text == computed(synced, request), role
        synced.run('BEGIN READ ONLY')
        try:
            synced.run(f'SET LOCAL ROLE {role}')
            info = json.loads(answer(synced, body('pixamind_api.get_info', {})))['result']
        finally:
            synced.run('ROLLBACK')
        assert info['feed_cache']['fresh'] == len(TARGETS), role


# ------------------------------------------------------------------ freshness
def test_new_blocks_refresh_the_cache(indexer, chain, sql):
    scenario.load(chain)
    for block in range(1, 9):
        indexer.process_live(block)
    before = rows(sql)
    chain.add(9, comment('carol', 'dog', parent_permlink='pixelart', title='Dog', body='woof'))
    indexer.process_live(9)
    after = rows(sql)
    assert all(block == 9 for block, *_ in after.values())
    created = json.loads(answer(sql, body(*CREATED)))['result']
    assert (created[0]['author'], created[0]['permlink']) == ('carol', 'dog')
    assert_served_and_identical(sql)
    # the created feeds changed at block 9; promoted did not (carol/dog is not promoted)
    assert after[key(*CREATED)][1] == 9
    assert after[key(*TARGETS[2])][1] == before[key(*TARGETS[2])][1] < 9

    chain.add(10, vote('dave', 'carol', 'dog'), effective_vote('dave', 'carol', 'dog', rshares=5000))
    indexer.process_live(10)
    trending = json.loads(answer(sql, body(*TRENDING)))['result']
    assert ('carol', 'dog') in [(p['author'], p['permlink']) for p in trending]
    assert rows(sql)[key(*TRENDING)][1] == 10
    assert_served_and_identical(sql)


def test_unchanged_feeds_only_move_to_the_new_block(synced, indexer, chain):
    before = rows(synced)
    hashes = dict(synced.all('SELECT method || params::text, hash FROM pixamind.feed_cache'))
    chain.add(9)  # an empty block
    indexer.process_live(9)
    after = rows(synced)
    assert {k: (9, *v[1:]) for k, v in before.items()} == after
    assert dict(synced.all('SELECT method || params::text, hash FROM pixamind.feed_cache')) == hashes
    assert_served_and_identical(synced)


def test_rows_of_another_block_are_never_served(synced):
    synced.run("SELECT pixamind_test.set_context(9, 9, 'live')")  # the context moved, no refresh yet
    for method, params in TARGETS:
        assert not cached(synced, method, params)
    synced.run("SELECT pixamind_test.set_context(8, 8, 'live')")
    assert_served_and_identical(synced)


def test_no_refresh_while_catching_up(synced, indexer, chain):
    chain.add(9)
    indexer.process_live(9, irreversible=40)  # 31 more blocks waiting: the next one follows at once
    assert {v[0] for v in rows(synced).values()} == {8}
    assert not any(cached(synced, m, p) for m, p in TARGETS)
    chain.add(10)
    indexer.process_live(10, irreversible=12)  # following the head again
    assert {v[0] for v in rows(synced).values()} == {10}
    assert_served_and_identical(synced)


def test_massive_batches_do_not_fill_the_cache(indexer, chain, sql):
    """Massive sync never refreshes (its batches commit in several steps and are
    followed by finalization); the first live block does."""
    scenario.load(chain)
    indexer.process_massive(1, 8)
    assert rows(sql) == {}
    for method, params in TARGETS:
        assert not cached(sql, method, params)
        assert 'result' in json.loads(answer(sql, body(method, params)))


def test_session_limit_must_match(synced):
    """hivemind_app.max_posts_per_call changes what a request answers: a session with
    another value than the indexer's computes."""
    method, params = TRENDING
    synced.run('BEGIN')
    try:
        synced.run("SET LOCAL hivemind_app.max_posts_per_call = '10'")
        served = cached(synced, method, params)
        response = json.loads(answer(synced, body(method, params)))
    finally:
        synced.run('ROLLBACK')
    assert not served
    assert 'error' in response  # limit 20 > 10, as without the cache


# ------------------------------------------------------------------ targets and failures
def test_failing_target_is_computed_and_reported(synced, indexer, chain):
    bad = {'sort': 'bogus', 'tag': '', 'limit': 20}
    synced.run(
        "INSERT INTO pixamind.feed_cache_targets (api, method, params) VALUES ('bridge', 'get_ranked_posts', %s)",
        json.dumps(bad),
    )
    chain.add(9)
    indexer.process_live(9)
    block, _, has_result, error = rows(synced)[key('bridge.get_ranked_posts', bad)]
    assert block == 9 and not has_result and 'Unsupported sort' in error
    assert not cached(synced, 'bridge.get_ranked_posts', bad)
    response = json.loads(answer(synced, body('bridge.get_ranked_posts', bad)))['error']
    expected = json.loads(computed(synced, body('bridge.get_ranked_posts', bad)))['error']
    assert (response['code'], response['message']) == (expected['code'], expected['message'])
    info = synced.result('pixamind_api.get_info')['feed_cache']
    assert (info['targets'], info['fresh'], info['failing']) == (len(TARGETS) + 1, len(TARGETS), 1)
    assert_served_and_identical(synced)


def test_clock_dependent_namespaces_are_refused(synced):
    for api, method in (('hive', 'db_head_state'), ('pixamind_api', 'get_metrics')):
        with pytest.raises(psycopg2.errors.CheckViolation):
            synced.run(
                "INSERT INTO pixamind.feed_cache_targets (api, method, params) VALUES (%s, %s, '{}')", api, method
            )


def test_methods_setting_http_headers_are_not_cached(synced, indexer, chain):
    """A Cache-Control header or a status set by a method could not be replayed from a row."""
    synced.run(
        'CREATE OR REPLACE FUNCTION hivemind_endpoints.bridge_api_get_trending_topics(_params JSONB) '
        'RETURNS JSONB LANGUAGE plpgsql STABLE AS $$ BEGIN '
        "PERFORM set_config('response.headers', '[{\"Cache-Control\": \"public, max-age=2\"}]', true); "
        "RETURN '[]'::JSONB; END $$"
    )
    synced.run(
        "INSERT INTO pixamind.feed_cache_targets (api, method, params) VALUES ('bridge', 'get_trending_topics', '{}')"
    )
    chain.add(9)
    indexer.process_live(9)
    block, _, has_result, error = rows(synced)[key('bridge.get_trending_topics', {})]
    assert block == 9 and not has_result and 'not cacheable' in error
    assert not cached(synced, 'bridge.get_trending_topics', {})
    assert_served_and_identical(synced)


def test_removed_and_disabled_targets_are_dropped(synced, indexer, chain):
    synced.run(
        "UPDATE pixamind.feed_cache_targets SET enabled = FALSE WHERE params = '{\"sort\": \"hot\", \"tag\": \"\", \"limit\": 20}'"
    )
    synced.run("DELETE FROM pixamind.feed_cache_targets WHERE method = 'get_discussions_by_created'")
    chain.add(9)
    indexer.process_live(9)
    left = rows(synced)
    assert len(left) == len(TARGETS) - 2
    assert not cached(synced, *TARGETS[1]) and not cached(synced, *CREATED)
    assert_served_and_identical(synced, [t for t in TARGETS if t not in (TARGETS[1], CREATED)])


def test_refresh_failure_never_stops_the_block(synced, indexer, chain):
    synced.run(
        'CREATE OR REPLACE FUNCTION pixamind.refresh_feed_cache(_max_pending INT DEFAULT 20) RETURNS JSONB '
        'LANGUAGE plpgsql AS '
        "$$ BEGIN RAISE EXCEPTION 'boom'; END $$"
    )
    chain.add(9, comment('carol', 'dog', parent_permlink='pixelart', title='Dog', body='woof'))
    indexer.process_live(9)
    assert synced.one('SELECT last_completed_block_num FROM hivemind_app.hive_state') == 9
    assert not any(cached(synced, m, p) for m, p in TARGETS)  # still block 8's rows: not served
    created = json.loads(answer(synced, body(*CREATED)))['result']
    assert created[0]['permlink'] == 'dog'  # computed, with the new block


def test_turned_off_and_restart(synced, indexer, chain):
    PixamindHooks.configure(None, indexer.db, PixamindSettings(feed_cache=False))
    assert not PixamindHooks.feed_cache and rows(synced) == {}
    chain.add(9)
    indexer.process_live(9)
    assert rows(synced) == {}
    info = synced.result('pixamind_api.get_info')
    assert info['features']['feed_cache'] is False and info['feed_cache']['enabled'] is False
    for method, params in TARGETS:
        request = body(method, params)
        assert 'result' in json.loads(answer(synced, request))
    # turned back on: a start always begins empty, the next block fills it
    PixamindHooks.configure(None, indexer.db, PixamindSettings())
    assert PixamindHooks.feed_cache and rows(synced) == {}
    chain.add(10)
    indexer.process_live(10)
    assert_served_and_identical(synced)


def test_api_works_without_the_pixamind_runtime(synced):
    """home.sql's default cached_result caches nothing: what the API runs on until (or
    unless) the Pixamind runtime is installed."""
    synced.run((ROOT / 'hive/db/sql_scripts/postgrest/home.sql').read_text())
    for method, params in TARGETS:
        assert not cached(synced, method, params)
        assert 'result' in json.loads(answer(synced, body(method, params)))


def test_get_info_reports_the_cache(synced):
    info = synced.result('pixamind_api.get_info')
    assert info['features']['feed_cache'] is True
    cache = info['feed_cache']
    assert cache['enabled'] is True and cache['block'] == 8 and cache['size_bytes'] > 0
    assert (cache['targets'], cache['fresh'], cache['failing']) == (len(TARGETS), len(TARGETS), 0)


# ------------------------------------------------------------------ JIT
def test_api_functions_run_without_jit(synced):
    functions = synced.all(
        "SELECT p.oid::regprocedure::text, p.proconfig FROM pg_proc p WHERE p.pronamespace = 'hivemind_endpoints'::regnamespace"
    )
    assert len(functions) > 40
    assert [name for name, config in functions if 'jit=off' not in (config or [])] == []
    jit = synced.result('pixamind_api.get_info')['database']['jit']
    assert jit['api'] == 'off'
    assert jit['database'] is None  # the test database has no database-level setting
    synced.run("DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET jit = off', current_database()); END $$")
    assert synced.result('pixamind_api.get_info')['database']['jit']['database'] == 'off'
