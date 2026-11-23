"""Post and comment version history (docs/pixamind/POST-HISTORY.md).

Versions are recorded where hivemind merges each comment operation
(hive/indexer/blocks.py -> pixamind/indexer/history.py), in live and massive
sync, and rebuilt from HAF by `pixamind history backfill`. Both must give the
same rows, and a post's latest version must be what hivemind stores.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import scenario
from chain import comment, delete_comment
from harness import app_dsn
from livesync import Chain, Indexer

from hive.indexer.post_data_cache import PostDataCache
from hive.indexer.posts import Posts
from pixamind.conf import PixamindSettings
from pixamind.indexer.hooks import PixamindHooks
from pixamind.tools import history

PATCH = '@@ -1,10 +1,10 @@\n-First\n+Final\n  post\n'  # bob/blog-1: "First post" -> "Final post"
NEWS = {'app': 'pixagram', 'tags': ['blog', 'news']}
COLUMNS = 'op_id, post_id, block_num, created_at, is_creation, title, body, json_metadata'
RUNTIME = Path(__file__).resolve().parents[2] / 'pixamind' / 'sql' / 'runtime' / '75_post_history.sql'


def edits_block_9():
    """Two edits of bob/blog-1 in one block (a patch, then the json only), and new posts."""
    return [
        comment('bob', 'blog-1', parent_permlink='blog', title='', body=PATCH, json_metadata=''),
        comment('bob', 'blog-1', parent_permlink='blog', title='', body='', json_metadata=NEWS),
        comment('carol', 'c1', parent_permlink='pixelart', title='C1', body='one'),
        comment('dave', 'd1', parent_permlink='pixelart', title='D1', body='never edited'),
    ]


def recreate_block_10():
    """carol/c1 deleted and posted again in one block, then edited."""
    return [
        delete_comment('carol', 'c1'),
        comment('carol', 'c1', parent_permlink='pixelart', title='C1 again', body='two'),
        comment('carol', 'c1', parent_permlink='pixelart', title='', body='three'),
    ]


def load(chain):
    scenario.load(chain)
    chain.add(9, *edits_block_9())
    chain.add(10, *recreate_block_10())


def history_of(sql, author, permlink, **params):
    return sql.result('pixamind_content.get_post_history', {'author': author, 'permlink': permlink, **params})


def summary(result):
    return [
        (v['version'], v['created'], v['changed'], v['title'], v.get('body'), v['json_metadata'])
        for v in result['versions']
    ]


def live_rows(sql):
    """Versions of the posts and comments that still exist (the backfill's scope)."""
    return sql.all(
        f'SELECT {COLUMNS} FROM pixamind.post_versions v '
        'WHERE EXISTS (SELECT 1 FROM hivemind_app.hive_posts hp WHERE hp.id = v.post_id AND hp.counter_deleted = 0) '
        'ORDER BY op_id'
    )


def assert_latest_is_stored(sql):
    differ = sql.all(
        """
        SELECT hp.id FROM hivemind_app.hive_posts hp
          JOIN hivemind_app.hive_post_data d ON d.id = hp.id
          CROSS JOIN LATERAL (SELECT s.title, s.body, s.json_metadata FROM pixamind.post_version_states(hp.id) s
                               ORDER BY s.version DESC LIMIT 1) last
         WHERE hp.counter_deleted = 0 AND hp.id > 0
           AND (last.title, last.body, last.json_metadata) IS DISTINCT FROM (d.title, d.body, d.json)
        """
    )
    assert differ == []


def backfill(sql, first=1, posts=2):
    out = io.StringIO()
    code = history.backfill(app_dsn(), first, posts, out)
    return code, out.getvalue()


@pytest.fixture
def synced(indexer, chain, sql):
    load(chain)
    for block in range(1, 11):
        indexer.process_live(block)
    return sql


# ------------------------------------------------------------------ recording
def test_creation_and_edits(synced):
    cat = history_of(synced, 'alice', 'cat', bodies=False)
    assert (cat['total'], cat['complete']) == (2, True)
    assert [(v['version'], v['created'], v['changed'], v['title']) for v in cat['versions']] == [
        (1, True, ['title', 'body', 'json_metadata'], 'Cat'),
        (2, False, ['title'], 'Cat (edited)'),
    ]
    assert 'body' not in cat['versions'][0]
    meta = synced.result('pixamind_content.get_content_metadata', {'author': 'alice', 'permlink': 'cat'})
    assert (cat['versions'][0]['timestamp'], cat['versions'][-1]['timestamp']) == (meta['created'], meta['updated'])
    assert [v['block_num'] for v in cat['versions']] == [2, 5]
    assert all(v['op_id'].isdigit() for v in cat['versions'])  # a string: op ids exceed 2^53


def test_patch_and_several_edits_in_one_block(synced):
    blog = history_of(synced, 'bob', 'blog-1')
    first_meta = json.dumps({'app': 'pixagram', 'format': 'text', 'tags': ['blog']})
    assert summary(blog) == [
        (1, True, ['title', 'body', 'json_metadata'], 'Hello', 'First post', first_meta),
        (2, False, ['body'], 'Hello', 'Final post', first_meta),  # the patch, applied as hivemind does
        (3, False, ['json_metadata'], 'Hello', 'Final post', json.dumps(NEWS)),
    ]
    # unchanged columns are not stored again
    stored = synced.all(
        'SELECT title, body, json_metadata FROM pixamind.post_versions WHERE block_num = 9 ORDER BY op_id'
    )
    assert stored[:2] == [(None, 'Final post', None), (None, None, json.dumps(NEWS))]
    assert_latest_is_stored(synced)


def test_nothing_is_stored_twice(synced):
    def stored(permlink):
        return synced.all(
            'SELECT v.is_creation, v.title, v.body IS NOT NULL, v.json_metadata IS NOT NULL '
            'FROM pixamind.post_versions v JOIN hivemind_app.hive_posts hp ON hp.id = v.post_id '
            'JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id '
            'WHERE hp.counter_deleted = 0 AND hpd.permlink = %s ORDER BY v.op_id',
            permlink,
        )

    # never edited: the creation row holds no text, the API reads hivemind's
    assert stored('d1') == [(True, None, False, False)]
    assert summary(history_of(synced, 'dave', 'd1')) == [
        (1, True, ['title', 'body', 'json_metadata'], 'D1', 'never edited', '')
    ]
    # edited: the creation row got the original at the first edit; the edit keeps only its new title
    assert stored('cat') == [(True, 'Cat', True, True), (False, 'Cat (edited)', False, False)]


def test_deleted_and_posted_again(synced):
    c1 = history_of(synced, 'carol', 'c1')
    assert summary(c1) == [
        (1, True, ['title', 'body', 'json_metadata'], 'C1 again', 'two', ''),
        (2, False, ['body'], 'C1 again', 'three', ''),
    ]
    deleted = synced.rpc('pixamind_content.get_post_history', {'author': 'bob', 'permlink': 're-cat'})
    assert 'was deleted' in deleted['error']['data']


def test_paging_and_validation(synced):
    page = history_of(synced, 'bob', 'blog-1', start=2, limit=1, bodies=False)
    assert (page['total'], [v['version'] for v in page['versions']]) == (3, [2])
    assert history_of(synced, 'bob', 'blog-1', start=9)['versions'] == []
    for params in (
        {'author': 'bob'},
        {'author': 'bob', 'permlink': 'blog-1', 'limit': 101},
        {'author': 'bob', 'permlink': 'blog-1', 'bodies': 'yes'},
        {'author': 'bob', 'permlink': 'blog-1', 'colour': 1},
    ):
        assert 'error' in synced.rpc('pixamind_content.get_post_history', params), params
    positional = synced.rpc('pixamind_content.get_post_history', ['bob', 'blog-1'])
    assert positional['result']['total'] == 3
    info = synced.result('pixamind_api.get_info')['features']
    assert (info['post_history'], info['post_history_backfilled']) == (True, False)


def test_massive_sync_records_every_version(indexer, chain, sql):
    load(chain)
    indexer.process_massive(1, 10)
    assert summary(history_of(sql, 'bob', 'blog-1'))[1:] == [
        (
            2,
            False,
            ['body'],
            'Hello',
            'Final post',
            json.dumps({'app': 'pixagram', 'format': 'text', 'tags': ['blog']}),
        ),
        (3, False, ['json_metadata'], 'Hello', 'Final post', json.dumps(NEWS)),
    ]
    assert [v['title'] for v in history_of(sql, 'carol', 'c1')['versions']] == ['C1 again', 'C1 again']
    assert_latest_is_stored(sql)


def test_turned_off(dsn, sql):
    idx = Indexer(dsn, PixamindSettings(post_history=False))
    try:
        load(Chain(idx.db))
        for block in range(1, 11):
            idx.process_live(block)
    finally:
        idx.close()
    assert sql.one('SELECT count(*) FROM pixamind.post_versions') == 0
    assert sql.result('pixamind_api.get_info')['features']['post_history'] is False
    assert history_of(sql, 'alice', 'cat') == {
        'author': 'alice',
        'permlink': 'cat',
        'total': 0,
        'complete': False,
        'versions': [],
    }


def test_failure_never_stops_the_block(indexer, chain, sql):
    load(chain)
    for block in range(1, 9):
        indexer.process_live(block)
    sql.run(
        'CREATE OR REPLACE FUNCTION pixamind.write_post_versions(_rows JSONB) RETURNS INT LANGUAGE plpgsql AS '
        "$$ BEGIN RAISE EXCEPTION 'boom'; END $$"
    )
    indexer.process_live(9)
    assert sql.one("SELECT body FROM hivemind_app.hive_post_data WHERE body = 'Final post'") == 'Final post'
    assert sql.one('SELECT count(*) FROM pixamind.post_versions WHERE block_num = 9') == 0
    assert sql.one("SELECT count(*) FROM pixamind.projection_failures WHERE projection = 'post_versions'") == 1
    # the next start reinstalls the function; the backfill repairs the range
    sql.run(RUNTIME.read_text())
    code, _ = backfill(sql, first=9)
    assert code == 0
    assert [v['body'] for v in history_of(sql, 'bob', 'blog-1')['versions']] == [
        'First post',
        'Final post',
        'Final post',
    ]


# ------------------------------------------------------------------ backfill
def test_backfill_rebuilds_what_live_sync_recorded(synced):
    recorded = live_rows(synced)
    synced.run('DELETE FROM pixamind.post_versions')
    code, out = backfill(synced)
    assert code == 0 and '0 differ from hivemind' in out
    assert live_rows(synced) == recorded
    assert synced.result('pixamind_api.get_info')['features']['post_history_backfilled'] is True
    # again, through the command: nothing to add
    assert history.main(['backfill', '--database-url', app_dsn(), '--posts', '1']) == 0
    assert live_rows(synced) == recorded


def test_backfill_completes_history_recorded_late(dsn, sql):
    """History turned on after block 6 (as on a node installed with the posts already
    there): bob/blog-1's first recorded version is an edit, complete only after the
    backfill."""
    idx = Indexer(dsn, PixamindSettings(post_history=False))
    try:
        load(Chain(idx.db))
        for block in range(1, 7):
            idx.process_live(block)
        PixamindHooks.configure(None, idx.db, PixamindSettings())
        for block in range(7, 11):
            idx.process_live(block)
    finally:
        idx.close()
    late = history_of(sql, 'bob', 'blog-1')
    assert (late['total'], late['complete']) == (2, False)
    assert late['versions'][0]['changed'] is None  # what it changed from is not known yet
    assert [v['body'] for v in late['versions']] == ['Final post', 'Final post']
    assert history_of(sql, 'alice', 'cat')['total'] == 0

    code, _ = backfill(sql)
    assert code == 0
    full = history_of(sql, 'bob', 'blog-1')
    assert (full['total'], full['complete']) == (3, True)
    assert [v['changed'] for v in full['versions']] == [['title', 'body', 'json_metadata'], ['body'], ['json_metadata']]
    assert [v['title'] for v in history_of(sql, 'alice', 'cat')['versions']] == ['Cat', 'Cat (edited)']
    assert_latest_is_stored(sql)


@pytest.mark.parametrize(
    ('old', 'sent'),
    [
        ('First post', PATCH),  # a patch that applies
        ('Something else', PATCH),  # no hunk matches: hivemind keeps the stored body
        ('First post', 'A whole new body'),  # not a patch
        ('First post', '@@ broken'),  # malformed patch header: taken as text
        ('', '@@ -0,0 +1,5 @@\n+Hello\n'),  # a patch onto an empty body
    ],
)
def test_merge_is_hivemind_merge(old, sent):
    PostDataCache._data[-1] = {'body': old}
    try:
        assert history.merge_body(old, sent) == Posts._merge_post_body(-1, sent)
    finally:
        PostDataCache._data.pop(-1, None)
