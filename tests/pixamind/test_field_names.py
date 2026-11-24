"""Operations as a Pixa node with hardfork 32 code writes them.

That node names the PXS and PIXA fields after Pixa (percent_pxs, pxs_payout, pixa_payout); earlier
nodes, and HAF built on them, use Hive's names (percent_hbd, hbd_payout, hive_payout). Pixamind reads
either, and its posts carry the field as percent_pxs.
"""

import pytest
from chain import account_create, author_reward, comment, comment_options

ACCOUNTS = ['alice', 'bob']


def load(chain) -> None:
    for name in ACCOUNTS:
        chain.add_account(1, name)
    chain.add(1, *[account_create('initminer', name) for name in ACCOUNTS])
    chain.add(
        2,
        comment('alice', 'pixa-names', parent_permlink='pixelart', title='Pixa names', body='one'),
        comment_options('alice', 'pixa-names', percent=5000, pixa_names=True),
        comment('bob', 'hive-names', parent_permlink='blog', title='Hive names', body='two'),
        comment_options('bob', 'hive-names', percent=2500),
    )
    chain.add(
        3,
        author_reward('alice', 'pixa-names', '0.100', '0.200', '1.000000', pixa_names=True),
        author_reward('bob', 'hive-names', '0.300', '0.400', '2.000000'),
    )


@pytest.fixture
def live(indexer, chain, sql):
    load(chain)
    for block in range(1, 4):
        indexer.process_live(block)
    return sql


@pytest.fixture
def massive(indexer, chain, sql):
    load(chain)
    indexer.process_massive(1, 3)
    return sql


def test_posts_carry_percent_pxs(live):
    for author, permlink, percent in (('alice', 'pixa-names', 5000), ('bob', 'hive-names', 2500)):
        found = live.result('database_api.find_comments', {'comments': [[author, permlink]]})['comments']
        for method, post in (
            ('bridge.get_post', live.result('bridge.get_post', {'author': author, 'permlink': permlink})),
            (
                'condenser_api.get_content',
                live.result('condenser_api.get_content', {'author': author, 'permlink': permlink}),
            ),
            ('database_api.find_comments', found[0]),
        ):
            assert post['percent_pxs'] == percent, (method, author)
            assert 'percent_hbd' not in post, method


@pytest.mark.parametrize('synced_name', ['live', 'massive'])
def test_percent_read_under_either_name(request, synced_name):
    """Live and massive sync read comment_options in SQL, each in its own statement
    (massive_sync.sql); the column keeps Hivemind's name."""
    sql = request.getfixturevalue(synced_name)
    assert sql.all(
        'SELECT ha.name, hpd.permlink, hp.percent_hbd FROM hivemind_app.hive_posts hp '
        'JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id '
        'JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id WHERE hp.id > 0 ORDER BY 1'
    ) == [('alice', 'pixa-names', 5000), ('bob', 'hive-names', 2500)]


@pytest.mark.parametrize('synced_name', ['live', 'massive'])
def test_author_reward_read_under_either_name(request, synced_name):
    """Event payloads keep the v1 keys (hbd_payout, hive_payout) whatever the node calls the fields."""
    rows = request.getfixturevalue(synced_name).all(
        "SELECT account, payload FROM pixamind.events WHERE type = 'author_reward' ORDER BY op_id"
    )
    assert rows == [
        (
            'alice',
            {
                'author': 'alice',
                'permlink': 'pixa-names',
                'hbd_payout': '0.100 PXS',
                'hive_payout': '0.200 PIXA',
                'vesting_payout': '1.000000 VESTS',
            },
        ),
        (
            'bob',
            {
                'author': 'bob',
                'permlink': 'hive-names',
                'hbd_payout': '0.300 PXS',
                'hive_payout': '0.400 PIXA',
                'vesting_payout': '2.000000 VESTS',
            },
        ),
    ]
