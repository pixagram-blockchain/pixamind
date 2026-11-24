"""End to end: the chain's native NFT operations through the Pixamind NFT projection.

Blocks 1-8 are the social scenario (scenario.py); the NFT history follows:

Block 9   alice mints alice/cat with 10,000 copies (10% royalty, a license of some 20 KB, 10,000 PXS)
          and offers copy 1 for 50 PXS, copy 2 for 280 PXS and copy 3 for 900 PXS in the same block
Block 10  bob buys copy 1 from the creator: no royalty
Block 11  bob offers it for 80 PXS, then lowers the price to 75 PXS
Block 12  carol buys it from bob: 7.500 PXS royalty to alice
Block 13  carol gives it to dave; alice burns copy 5, which nothing had happened to
Block 14  dave offers copy 1 for 300 PXS; bob mints bob/blog-1 with no royalty, offers it for
          1 PXS and withdraws the offer (the copy never left him: it is back in his reserve)
Block 15  carol buys copy 1 back from dave: 30.000 PXS royalty to alice; alice gives copy 10000,
          untouched until then, to eve
"""

import hashlib

import pytest
import scenario
from chain import (
    comment,
    nft_buy,
    nft_list,
    nft_mint,
    nft_royalty,
    nft_sold,
    nft_transfer,
    nft_unlist,
)

LICENSE = (
    'Pixagram license 1.0 — édition complète\n\n'
    + ('The holder of a copy may display it, lend it and resell it; the creator keeps the copyright. ') * 220
)
LICENSE_SHA256 = hashlib.sha256(LICENSE.encode()).hexdigest()

NFT_BLOCKS = {
    9: [
        nft_mint('alice', 'cat', royalty=1000, license=LICENSE, editions=10000),
        nft_list('alice', 'alice', 'cat', '50.000'),
        nft_list('alice', 'alice', 'cat', '280.000', edition=2),
        nft_list('alice', 'alice', 'cat', '900.000', edition=3),
    ],
    10: [nft_buy('bob', 'alice', 'cat', '50.000'), nft_sold('alice', 'bob', 'alice', 'cat', '50.000')],
    11: [nft_list('bob', 'alice', 'cat', '80.000'), nft_list('bob', 'alice', 'cat', '75.000')],
    12: [
        nft_buy('carol', 'alice', 'cat', '75.000'),
        nft_sold('bob', 'carol', 'alice', 'cat', '75.000', '7.500'),
        nft_royalty('alice', 'bob', 'carol', 'cat', '7.500'),
    ],
    13: [nft_transfer('carol', 'alice', 'cat', 'dave'), nft_transfer('alice', 'alice', 'cat', 'null', edition=5)],
    14: [
        nft_list('dave', 'alice', 'cat', '300.000'),
        nft_mint('bob', 'blog-1'),
        nft_list('bob', 'bob', 'blog-1', '1.000'),
        nft_unlist('bob', 'bob', 'blog-1'),
    ],
    15: [
        nft_buy('carol', 'alice', 'cat', '300.000'),
        nft_sold('dave', 'carol', 'alice', 'cat', '300.000', '30.000'),
        nft_royalty('alice', 'dave', 'carol', 'cat', '30.000'),
        nft_transfer('alice', 'alice', 'cat', 'eve', edition=10000),
    ],
}
LAST_BLOCK = 15


def load(chain) -> None:
    scenario.load(chain)
    for block, ops in sorted(NFT_BLOCKS.items()):
        chain.add(block, *ops)


@pytest.fixture
def synced(indexer, chain, sql):
    load(chain)
    for block in range(1, LAST_BLOCK + 1):
        indexer.process_live(block)
    return sql


def tables(sql) -> tuple:
    return (
        sql.all('SELECT * FROM pixamind.nft_events ORDER BY op_id'),
        sql.all('SELECT * FROM pixamind.nfts ORDER BY author, permlink'),
        sql.all('SELECT * FROM pixamind.nft_editions ORDER BY author, permlink, edition'),
    )


def test_projection(synced):
    assert synced.one('SELECT count(*) FROM pixamind.projection_failures') == 0
    types = [row[0] for row in synced.all('SELECT type FROM pixamind.nft_events ORDER BY op_id')]
    # a purchase is recorded once, from its nft_sold operation; a transfer to null is a burn
    assert types == [
        'mint', 'list', 'list', 'list',
        'sale',
        'list', 'list',
        'sale',
        'transfer', 'burn',
        'list', 'mint', 'list', 'unlist',
        'sale', 'transfer',
    ]  # fmt: skip
    assert synced.all(
        'SELECT author, permlink, editions, royalty_bps, license_size, license_sha256, mint_fee, post_id IS NOT NULL '
        'FROM pixamind.nfts ORDER BY 1'
    ) == [
        ('alice', 'cat', 10000, 1000, len(LICENSE.encode()), LICENSE_SHA256, 10000, True),
        ('bob', 'blog-1', 1, 0, 0, hashlib.sha256(b'').hexdigest(), 1, True),
    ]
    # a row only for the copies something happened to: the other 9,995 copies of cat are alice's;
    # bob's copy has none: he withdrew the offer of a copy that never left him, so it is in his reserve
    assert synced.all('SELECT permlink, edition, owner, price, sales FROM pixamind.nft_editions ORDER BY 1, 2') == [
        ('cat', 1, 'carol', None, 3),
        ('cat', 2, 'alice', 280, 0),
        ('cat', 3, 'alice', 900, 0),
        ('cat', 5, 'null', None, 0),
        ('cat', 10000, 'eve', None, 0),
    ]
    volume, royalties = synced.all(
        "SELECT volume, royalties FROM pixamind.nft_editions WHERE permlink = 'cat' AND edition = 1"
    )[0]
    assert (volume, royalties) == (425, 37.5)


def test_massive_sync_gives_the_same_tables(indexer, chain, sql):
    load(chain)
    for block in range(1, LAST_BLOCK + 1):
        indexer.process_live(block)
    live = tables(sql)
    sql.run('TRUNCATE pixamind.nft_events, pixamind.nfts, pixamind.nft_editions')
    sql.run(f'SELECT pixamind.process_nfts(1, {LAST_BLOCK})')
    assert tables(sql) == live
    # replaying part of the range changes nothing
    sql.run('SELECT pixamind.process_nfts(10, 13)')
    assert tables(sql) == live


def test_ids_say_what_is_minted(synced):
    assert synced.result('pixamind_nft.get_nfts', {'nft': '@carol/nothing/0'}) == {'nfts': []}
    found = synced.result(
        'pixamind_nft.get_nfts',
        {
            'nfts': [
                '@bob/blog-1/0',
                '@alice/nope/0',
                '@alice/cat/0',
                '@alice/cat/3',
                '@alice/cat/10001',
                '@alice/cat/9999',
                '@alice/cat/0',
            ]
        },
    )
    # only what exists, once each, in the order first asked; copy 9999 exists although nothing happened to it
    assert [n['nft_id'] for n in found['nfts']] == ['@bob/blog-1/0', '@alice/cat/0', '@alice/cat/3', '@alice/cat/9999']


def test_an_nft(synced):
    (cat,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/0'})['nfts']
    assert (cat['editions'], cat['royalty'], cat['royalty_percent'], cat['mint_fee']) == (
        10000,
        1000,
        '10.00',
        '10000.000 PXS',
    )
    assert (cat['license_size'], cat['license_sha256']) == (len(LICENSE.encode()), LICENSE_SHA256)
    assert 'license' not in cat  # the text: get_license
    assert (cat['for_sale'], cat['burned'], cat['held_by_creator'], cat['sales']) == (2, 1, 9997, 3)
    assert cat['floor'] == {'price': '280.000 PXS', 'copies': 1, 'nft_ids': ['@alice/cat/2']}
    assert (cat['copies'], cat['listed'], cat['reserve']) == (10000, 2, 9995)
    assert 'reserve_offer' not in cat  # alice has not offered her reserve
    assert (cat['last_sale']['price'], cat['last_sale']['edition']) == ('300.000 PXS', 1)
    assert (cat['volume'], cat['royalties']) == ('425.000 PXS', '37.500 PXS')
    assert 'editions_list' not in cat  # the copies: list_editions


def test_copies(synced):
    page = synced.result('pixamind_nft.list_editions', {'nft': '@alice/cat/0', 'limit': 5})
    assert [(e['edition'], e['owner'], e['burned']) for e in page['editions']] == [
        (1, 'carol', False),
        (2, 'alice', False),
        (3, 'alice', False),
        (4, 'alice', False),
        (5, 'null', True),
    ]
    assert (page['next_start'], page['has_more']) == ('6', True)
    last = synced.result('pixamind_nft.list_editions', {'nft': '@alice/cat/0', 'start': '9999'})
    assert [(e['edition'], e['owner']) for e in last['editions']] == [(9999, 'alice'), (10000, 'eve')]
    for_sale = synced.result('pixamind_nft.list_editions', {'nft': '@alice/cat/0', 'sort': 'price'})
    assert [(e['edition'], e['price']) for e in for_sale['editions']] == [(2, '280.000 PXS'), (3, '900.000 PXS')]
    of_eve = synced.result('pixamind_nft.list_editions', {'nft': '@alice/cat/0', 'owner': 'eve'})
    assert [e['nft_id'] for e in of_eve['editions']] == ['@alice/cat/10000']
    (untouched,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/9999'})['nfts']
    assert (untouched['owner'], untouched['listed'], untouched['sales']) == ('alice', False, 0)


def test_one_copy(synced):
    (copy,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/3'})['nfts']
    assert (copy['owner'], copy['listed'], copy['price']) == ('alice', True, '900.000 PXS')
    assert copy['nft']['nft_id'] == '@alice/cat/0'  # the terms of its NFT come with it
    (first,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/1'})['nfts']
    assert (first['sales'], first['volume'], first['royalties'], first['last_sale_price']) == (
        3,
        '425.000 PXS',
        '37.500 PXS',
        '300.000 PXS',
    )


def test_lists(synced):
    created = synced.result('pixamind_nft.list_created', {'account': 'alice'})
    assert created['total'] == 1 and [n['nft_id'] for n in created['nfts']] == ['@alice/cat/0']

    def held(account: str, **params) -> list:
        nfts = synced.result('pixamind_nft.list_owned', {'account': account, **params})['nfts']
        return [(n['nft']['nft_id'], n['held'], n['listed']) for n in nfts]

    assert held('alice') == [('@alice/cat/0', 9997, 2)]  # the copies she never parted with
    assert held('alice', acquired_only=True) == []
    assert held('carol') == [('@alice/cat/0', 1, 0)]
    assert held('eve') == [('@alice/cat/0', 1, 0)]
    assert held('bob', acquired_only=True) == []
    assert held('dave') == []
    assert held('null') == [('@alice/cat/0', 1, 0)]
    recent = synced.result('pixamind_nft.list_market', {})
    assert [e['nft_id'] for e in recent['editions']] == ['@alice/cat/3', '@alice/cat/2']
    by_price = synced.result('pixamind_nft.list_market', {'sort': 'price'})
    assert [(e['nft_id'], e['price']) for e in by_price['editions']] == [
        ('@alice/cat/2', '280.000 PXS'),
        ('@alice/cat/3', '900.000 PXS'),
    ]


def test_history(synced):
    history = synced.result('pixamind_nft.get_history', {'nft': '@alice/cat/0'})['history']
    assert [(h['type'], h['account'], h.get('edition')) for h in history] == [
        ('transfer', 'alice', 10000),
        ('sale', 'carol', 1),
        ('list', 'dave', 1),
        ('burn', 'alice', 5),
        ('transfer', 'carol', 1),
        ('sale', 'carol', 1),
        ('list', 'bob', 1),
        ('list', 'bob', 1),
        ('sale', 'bob', 1),
        ('list', 'alice', 3),
        ('list', 'alice', 2),
        ('list', 'alice', 1),
        ('mint', 'alice', None),
    ]
    assert (history[1]['seller'], history[1]['price'], history[1]['royalty']) == ('dave', '300.000 PXS', '30.000 PXS')
    assert history[4]['to'] == 'dave'
    mint = history[-1]
    assert (mint['fee'], mint['license_sha256'], 'license' in mint) == ('10000.000 PXS', LICENSE_SHA256, False)
    one_copy = synced.result('pixamind_nft.get_history', {'nft': '@alice/cat/5'})['history']
    assert [(h['type'], h['nft_id']) for h in one_copy] == [('burn', '@alice/cat/5'), ('mint', '@alice/cat/0')]
    of_bob = synced.result('pixamind_nft.get_history', {'account': 'bob', 'limit': 3})
    assert of_bob['has_more'] is True and len(of_bob['history']) == 3


def test_license(synced):
    license = synced.result('pixamind_nft.get_license', {'nft': '@alice/cat/7'})
    assert license == {
        'nft_id': '@alice/cat/0',
        'license': LICENSE,
        'license_size': len(LICENSE.encode()),
        'license_sha256': LICENSE_SHA256,
    }
    assert synced.result('pixamind_nft.get_license', {'nft': '@carol/nothing/0'}) == {}


def test_every_post_says_if_it_is_minted(indexer, chain, sql):
    load(chain)
    chain.add(
        9, comment('carol', 're-cat', parent_author='alice', parent_permlink='cat', body='Wow')
    )  # bob's is deleted
    for block in range(1, 9):
        indexer.process_live(block)
    ranked = sql.result('bridge.get_ranked_posts', {'sort': 'created', 'tag': '', 'limit': 20})
    assert ranked and all(post['nft_minted'] is False for post in ranked)
    for block in range(9, LAST_BLOCK + 1):
        indexer.process_live(block)
    ranked = sql.result('bridge.get_ranked_posts', {'sort': 'created', 'tag': '', 'limit': 20})
    assert {(p['author'], p['permlink']): p['nft_minted'] for p in ranked}[('alice', 'cat')] is True
    assert sql.result('bridge.get_post', {'author': 'alice', 'permlink': 'cat'})['nft_minted'] is True
    assert sql.result('bridge.get_post', {'author': 'carol', 'permlink': 're-cat'})['nft_minted'] is False
    assert sql.result('condenser_api.get_content', {'author': 'alice', 'permlink': 'cat'})['nft_minted'] is True
    found = sql.result('database_api.find_comments', {'comments': [['alice', 'cat'], ['carol', 're-cat']]})['comments']
    assert {c['permlink']: c['nft_minted'] for c in found} == {'cat': True, 're-cat': False}
    discussions = sql.result('condenser_api.get_discussions_by_created', [{'tag': '', 'limit': 20}])
    assert all('nft_minted' in post for post in discussions)


def test_content_metadata_carries_the_nft(indexer, chain, sql):
    load(chain)
    for block in range(1, 9):
        indexer.process_live(block)
    before = sql.result('pixamind_content.get_content_metadata', {'author': 'alice', 'permlink': 'cat'})
    assert 'nft' not in before  # not minted yet
    for block in range(9, LAST_BLOCK + 1):
        indexer.process_live(block)
    after = sql.result('pixamind_content.get_content_metadata', {'author': 'alice', 'permlink': 'cat'})
    assert (after['nft']['nft_id'], after['nft']['royalty'], after['nft']['for_sale']) == ('@alice/cat/0', 1000, 2)
    assert 'license' not in after['nft']


def test_info_announces_the_namespace(synced):
    info = synced.result('pixamind_api.get_info')
    assert 'pixamind_nft' in info['namespaces'] and info['features']['nfts'] is True
    assert {'nft_minted', 'nft_listed', 'nft_unlisted', 'nft_sold', 'nft_transferred', 'nft_burned'} <= set(
        info['event_types']
    )


def test_errors(synced):
    assert 'error' in synced.rpc('pixamind_nft.list_created', {'limit': 5})
    assert 'error' in synced.rpc('pixamind_nft.list_created', {'account': 'alice', 'limit': 101})
    assert 'error' in synced.rpc('pixamind_nft.list_market', {'sort': 'random'})
    assert 'error' in synced.rpc('pixamind_nft.list_market', {'symbol': 'PXS'})  # prices are in PXS only
    assert 'error' in synced.rpc('pixamind_nft.list_editions', {'limit': 5})  # needs an NFT
    assert 'error' in synced.rpc('pixamind_nft.get_nfts', {'nft': 'alice/cat/0'})
    assert 'error' in synced.rpc('pixamind_nft.get_nfts', {'nfts': [f'@alice/p{n}/0' for n in range(101)]})
    assert 'error' in synced.rpc('pixamind_nft.mint', {})


def test_reserve(synced, chain, indexer):
    """The creator offers every copy that never left her at one price; buyers get the next one."""
    chain.add(16, nft_list('alice', 'alice', 'cat', '10.000', edition=0))
    # the node gives the lowest-numbered copy without state: 1, 2, 3 and 5 have some, so copy 4
    chain.add(
        17,
        nft_buy('dave', 'alice', 'cat', '10.000', edition=0),
        nft_sold('alice', 'dave', 'alice', 'cat', '10.000', edition=4),
    )
    for block in (16, 17):
        indexer.process_live(block)
    (cat,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/0'})['nfts']
    assert (cat['reserve'], cat['listed'], cat['for_sale']) == (9994, 2, 2 + 9994)
    offer = cat['reserve_offer']
    assert (offer['nft_id'], offer['price'], offer['copies'], offer['next']) == (
        '@alice/cat/0',
        '10.000 PXS',
        9994,
        '@alice/cat/6',
    )
    assert cat['floor'] == {'price': '10.000 PXS', 'copies': 9994, 'nft_ids': ['@alice/cat/0']}
    (sold,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/4'})['nfts']
    assert (sold['owner'], sold['sales'], sold['volume'], sold['royalties']) == ('dave', 1, '10.000 PXS', '0.000 PXS')
    market = synced.result('pixamind_nft.list_market', {'sort': 'price', 'creator': 'alice'})
    assert [e['nft_id'] for e in market['editions']] == ['@alice/cat/0', '@alice/cat/2', '@alice/cat/3']
    (reserved,) = synced.result('pixamind_nft.get_nfts', {'nft': '@alice/cat/6'})['nfts']
    assert (reserved['owner'], reserved.get('reserve')) == ('alice', True)
