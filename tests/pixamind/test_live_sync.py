"""End to end: synthetic Pixa blocks through the real hivemind block processor.

The scenario (scenario.py) is indexed block by block by Blocks.process_live_block_sql,
with the Pixamind hooks, then read back through the JSON-RPC API.
"""

import pytest
import scenario


@pytest.fixture
def synced(indexer, chain, sql):
    scenario.load(chain)
    for block in range(1, 7):
        indexer.process_live(block)
    return sql


def events(sql, **where):
    """Event rows (type, account, target, block_num, payload) matching the given columns."""
    rows = sql.all('SELECT type, account, target, block_num, payload FROM pixamind.events ORDER BY op_id, seq')
    columns = ('type', 'account', 'target', 'block_num')
    return [r for r in rows if all(dict(zip(columns, r[:4], strict=True))[k] == v for k, v in where.items())]


# ------------------------------------------------------------------ events
def test_event_stream(synced):
    types = [row[0] for row in events(synced)]
    assert types == [
        'account_created',
        'account_created',
        'account_created',
        'account_created',
        'post_created',
        'post_created',
        'vote',
        'comment_created',
        'follow',
        'follow',
        'follow',
        'reblog',
        'follow',
        'promotion',
        'promotion',
        'post_updated',
        'post_deleted',
        'account_updated',
        'author_reward',
        'promotion',
    ]
    assert synced.one('SELECT count(*) FROM pixamind.projection_failures') == 0


def test_event_payloads(synced):
    (artwork,) = [e for e in events(synced, type='post_created') if e[1] == 'alice']
    content = artwork[4]['content']
    assert artwork[4]['category'] == 'pixelart'
    assert content['format'] == 'image' and content['tags'] == ['pixelart', 'cat']
    assert content['image'] == {
        'mime': 'image/webp',
        'width': 128,
        'height': 96,
        'aspect_ratio': 1.3333,
        'animated': False,
        'alpha': True,
        'bytes': len(scenario.ARTWORK) * 3 // 4,
    }

    (mute,) = events(synced, type='follow', account='dave')
    assert mute[2] == 'alice' and mute[4]['action'] == 'mute'

    reward = events(synced, type='author_reward')[0][4]
    assert reward == {
        'author': 'bob',
        'permlink': 'blog-1',
        'hbd_payout': '0.100 PXS',
        'hive_payout': '0.000 PIXA',
        'vesting_payout': '1.000000 VESTS',
    }

    (reply,) = events(synced, type='comment_created')
    assert reply[1:3] == ('bob', 'alice') and reply[4]['parent_permlink'] == 'cat'


def test_event_ids_follow_chain_order(synced):
    ids = synced.all('SELECT op_id, seq, block_num FROM pixamind.events ORDER BY op_id, seq')
    blocks = [r[2] for r in ids]
    assert blocks == sorted(blocks)
    # op ids carry the block number in their high 32 bits (HAF layout)
    assert all(op_id >> 32 == block for op_id, _, block in ids)


# -------------------------------------------------------------- promotions
def test_promotion_totals(synced):
    rows = synced.all(
        """SELECT ha.name, hpd.permlink, pp.promoted::TEXT
             FROM pixamind.promoted_posts pp
             JOIN hivemind_app.hive_posts hp ON hp.id = pp.post_id
             JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
             JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
            ORDER BY pp.promoted DESC"""
    )
    assert rows == [('bob', 'blog-1', '2.000'), ('alice', 'cat', '1.750')]


def test_promoted_feed_bridge(synced):
    posts = synced.result('bridge.get_ranked_posts', {'sort': 'promoted'})
    assert [(p['author'], p['permlink'], p['promoted']) for p in posts] == [
        ('bob', 'blog-1', '2.000 PXS'),
        ('alice', 'cat', '1.750 PXS'),
    ]
    page2 = synced.result(
        'bridge.get_ranked_posts', {'sort': 'promoted', 'start_author': 'bob', 'start_permlink': 'blog-1', 'limit': 1}
    )
    assert [(p['author'], p['permlink']) for p in page2] == [('alice', 'cat')]
    by_tag = synced.result('bridge.get_ranked_posts', {'sort': 'promoted', 'tag': 'cat'})
    assert [p['permlink'] for p in by_tag] == ['cat']
    # dave muted alice: her post disappears from his promoted feed
    for_dave = synced.result('bridge.get_ranked_posts', {'sort': 'promoted', 'observer': 'dave'})
    assert [p['author'] for p in for_dave] == ['bob']


def test_promoted_feed_condenser_and_pixamind_feed(synced):
    condenser = synced.result('condenser_api.get_discussions_by_promoted', {'tag': '', 'limit': 10})
    assert [(p['author'], p['promoted']) for p in condenser] == [('bob', '2.000 PXS'), ('alice', '1.750 PXS')]
    feed = synced.result('pixamind_feed.get_promoted_posts', {'limit': 1})
    assert [p['author'] for p in feed] == ['bob']


def test_promotion_payments_api(synced):
    post = synced.result('pixamind_feed.get_promotions', {'author': 'alice', 'permlink': 'cat'})
    assert post['promoted'] == '1.750 PXS'
    assert [(p['sender'], p['amount']) for p in post['promotions']] == [('carol', '0.250 PXS'), ('carol', '1.500 PXS')]
    spender = synced.result('pixamind_feed.get_promotions', {'sender': 'dave'})
    # the liquid-token transfer and the memo without '@' are not promotions
    assert spender['total'] == '2.000 PXS' and len(spender['promotions']) == 1


def test_bad_sort_lists_promoted(synced):
    error = synced.error('bridge.get_ranked_posts', {'sort': 'nope'})
    assert 'promoted' in error['message']


# ------------------------------------------------------------------ the API
def test_get_events_paging_and_filters(synced):
    seen, cursor = [], None
    while True:
        params = {'limit': 4}
        if cursor:
            params['cursor'] = cursor
        page = synced.result('pixamind_api.get_events', params)
        seen += page['events']
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    assert len(seen) == synced.one('SELECT count(*) FROM pixamind.events')
    assert len({e['id'] for e in seen}) == len(seen)

    promos = synced.result('pixamind_api.get_events', {'types': ['promotion']})['events']
    assert [e['data']['amount'] for e in promos] == ['1.500 PXS', '2.000 PXS', '0.250 PXS']

    about_bob = synced.result('pixamind_api.get_events', {'accounts': ['bob'], 'limit': 100})['events']
    assert all('bob' in (e['account'], e['target']) for e in about_bob)
    assert {e['type'] for e in about_bob} >= {'vote', 'follow', 'promotion', 'author_reward'}

    error = synced.error('pixamind_api.get_events', {'types': ['nope']})
    assert 'unknown event type' in error['message']
    for bad in ('garbage', '-5-0', '99999999999999999999-0', '1-99999'):
        error = synced.error('pixamind_api.get_events', {'cursor': bad})
        assert 'invalid cursor' in error['message'], bad


def test_get_events_respects_irreversibility(indexer, chain, sql):
    scenario.load(chain)
    for block in range(1, 6):
        indexer.process_live(block)
    indexer.process_live(6, irreversible=5)  # block 6 processed but still reversible
    irreversible = sql.result('pixamind_api.get_events', {'limit': 1000})
    assert irreversible['upto_block'] == 5
    assert max(e['block_num'] for e in irreversible['events']) == 5
    everything = sql.result('pixamind_api.get_events', {'limit': 1000, 'irreversible_only': False})
    assert max(e['block_num'] for e in everything['events']) == 6


def test_info_and_metrics(synced):
    info = synced.result('pixamind_api.get_info')
    assert info['chain'] == {'name': 'pixa', 'liquid_symbol': 'PIXA', 'stable_symbol': 'PXS', 'vests_symbol': 'VESTS'}
    assert info['features']['events'] is True and info['database']['pixamind_migration'] >= 1
    metrics = synced.result('pixamind_api.get_metrics', {'window_minutes': 60})
    assert metrics['throughput']['batches'] == 6 and metrics['throughput']['blocks'] == 6
    assert metrics['live_latency_seconds']['samples'] == 6
    assert metrics['last_batch']['last_block'] == 6
    assert metrics['failures'] == {'open': 0, 'latest': []}
    assert metrics['promoted_posts'] == 2


def test_content_and_graph_api(synced):
    cat = synced.result('pixamind_content.get_content_metadata', {'author': 'alice', 'permlink': 'cat'})
    assert cat['title'] == 'Cat (edited)' and cat['content']['image']['width'] == 128
    batch = synced.result(
        'pixamind_content.get_content_metadata', {'posts': [['alice', 'cat'], {'author': 'bob', 'permlink': 'nope'}]}
    )
    assert batch[0]['permlink'] == 'cat' and batch[1] is None
    assert synced.error('pixamind_content.get_content_metadata', {'author': 'bob', 'permlink': 'nope'})

    assert synced.result('pixamind_graph.get_mutual_follows', {'account': 'alice'}) == ['bob']
    rel = synced.result('pixamind_graph.get_relationships', {'observer': 'dave', 'accounts': ['alice', 'bob']})
    assert rel['alice']['ignores'] is True and rel['alice']['follows'] is False
    assert rel['bob'] == {
        'follows': False,
        'followed_by': False,
        'ignores': False,
        'blacklists': False,
        'follows_blacklists': False,
        'follows_muted': False,
    }
    rel = synced.result('pixamind_graph.get_relationships', {'observer': 'alice', 'accounts': ['bob', 'carol']})
    assert (
        rel['bob']['follows']
        and rel['bob']['followed_by']
        and rel['carol']['followed_by']
        and not rel['carol']['follows']
    )


# ----------------------------------------------------------- Pixa fixes
def test_no_steem_hf1_rshares_upscale(synced):
    # Hivemind multiplies rshares of votes before block 905693 by 10^6 (Steem HF1);
    # on Pixa those blocks are the chain's first month and must stay as they are.
    assert synced.one("SELECT rshares FROM hivemind_app.hive_votes WHERE block_num = 3") == 1000


def test_curation_share_is_pixas(synced):
    assert synced.one('SELECT hivemind_postgrest_utilities.hive_curation_rewards_percent()') == 4000


def test_payout_amounts_use_chain_symbols(synced):
    # alice/cat carries comment_options: its max_accepted_payout went through
    # hivemind_app.legacy_amount, which hardcoded "HBD" before Pixamind
    post = synced.result('bridge.get_post', {'author': 'alice', 'permlink': 'cat'})
    assert post['max_accepted_payout'] == '1000000.000 PXS'
    assert post['beneficiaries'] == [{'account': 'dave', 'weight': 900}]
    assert (
        synced.one("SELECT hivemind_app.legacy_amount('{\"amount\":\"1500\",\"precision\":3,\"nai\":\"@@000000013\"}')")
        == '1.500 PXS'
    )


# ------------------------------------------------- idempotency and forks
def _reprocess_pixamind(sql, block):
    """Re-run Pixamind's step for one block, the way a replayed batch does."""
    sql.run('SELECT hivemind_app.load_ops_staging(%s, %s)', block, block)
    return sql.one("SELECT pixamind.after_batch('live', %s, %s, 0, '{}', '{\"events\": true}')", block, block)


def test_replay_is_idempotent(synced):
    before = synced.all(
        "SELECT * FROM pixamind.projection_fingerprints(ARRAY['events', 'promotions', 'promoted_posts'])"
    )
    for block in range(1, 7):
        summary = _reprocess_pixamind(synced, block)
        assert summary['failures'] == 0
    after = synced.all(
        "SELECT * FROM pixamind.projection_fingerprints(ARRAY['events', 'promotions', 'promoted_posts'])"
    )
    assert before == after


def test_microfork_converges(synced):
    # Block 6 is replaced by another fork's block 6: carol's 0.250 promotion never
    # happened there, dave promotes alice/cat with 3.000 instead.
    synced.run('DELETE FROM hafd.operations WHERE block_num = 6')
    synced.run("""SELECT pixamind_test.add_op(6, 2::SMALLINT,
                  '{"from": "dave", "to": "null", "amount": {"amount": "3000", "precision": 3, "nai": "@@000000013"}, "memo": "@alice/cat"}')""")
    _reprocess_pixamind(synced, 6)
    promos = synced.all("SELECT sender, amount::TEXT FROM pixamind.post_promotions WHERE block_num = 6")
    assert promos == [('dave', '3.000')]
    total = synced.result('pixamind_feed.get_promotions', {'author': 'alice', 'permlink': 'cat'})['promoted']
    assert total == '4.500 PXS'
    block6 = [e[4] for e in events(synced, block_num=6)]
    assert block6 == [
        {'sender': 'dave', 'author': 'alice', 'permlink': 'cat', 'amount': '3.000 PXS', 'post_exists': True}
    ]


def test_fingerprints_cover_every_projection(synced):
    rows = synced.all('SELECT projection, rows, fingerprint FROM pixamind.projection_fingerprints()')
    names = {r[0] for r in rows}
    assert names == set(synced.one('SELECT pixamind.fingerprint_projections()'))
    counts = dict((r[0], r[1]) for r in rows)
    assert counts['posts'] == 3 and counts['votes'] == 1 and counts['follows'] == 3 and counts['muted'] == 1
    assert counts['promotions'] == 3 and counts['promoted_posts'] == 2
    buckets = synced.all("SELECT * FROM pixamind.projection_bucket_fingerprints('events', 16)")
    assert sum(b[1] for b in buckets) == counts['events']
    bucket = buckets[0][0]
    assert len(synced.all("SELECT * FROM pixamind.projection_rows('events', %s, 16)", bucket)) == buckets[0][1]


def test_community_events(indexer, chain, sql):
    scenario.load(chain)
    for block in range(1, 9):
        indexer.process_live(block)
    community = sql.result('bridge.get_community', {'name': 'portal-100001'})
    assert community['name'] == 'portal-100001'
    subscribers = [row[0] for row in sql.result('bridge.list_subscribers', {'community': 'portal-100001'})]
    assert 'alice' in subscribers
    rows = events(sql, type='community')
    assert [(r[1], r[4]['action'], r[4]['community']) for r in rows] == [
        ('alice', 'subscribe', 'portal-100001'),
        ('bob', 'subscribe', 'portal-100001'),  # a parameter named "action" cannot spoof the action
    ]
