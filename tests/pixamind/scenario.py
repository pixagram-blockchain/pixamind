"""A small Pixa social history, used by the end-to-end tests.

Block 1  accounts alice, bob, carol, dave (created by initminer)
Block 2  alice posts an artwork (WebP 128x96) in #pixelart with comment_options (front-end
         beneficiary, max payout 1000000.000); bob posts a text blog in #blog
Block 3  bob votes alice/cat (effective, 1000 rshares) and replies; follows: bob->alice,
         carol->alice, alice->bob
Block 4  carol reblogs alice/cat; dave mutes alice; promotions: carol 1.500 PXS for
         alice/cat, dave 2.000 PXS for bob/blog-1, plus three transfers that are not
         promotions (liquid token, bad memo, not to @null)
Block 5  alice edits her post; bob deletes his reply; carol updates her profile;
         bob/blog-1 is paid out (author_reward)
Block 6  carol promotes alice/cat again (0.250 PXS)
Block 7  initminer creates the community account portal-100001
Block 8  alice and bob subscribe to it (bob's operation carries a spoofed "action" key)
"""

from __future__ import annotations

from chain import (
    LIQUID_NAI,
    account_create,
    account_update2,
    author_reward,
    comment,
    comment_options,
    community_op,
    delete_comment,
    effective_vote,
    follow,
    reblog,
    transfer,
    vote,
    webp_vp8x_b64,
)

ACCOUNTS = ['alice', 'bob', 'carol', 'dave']
ARTWORK = webp_vp8x_b64(128, 96)
ARTWORK_META = {
    'app': 'pixagram/3.0.2',
    'format': 'image',
    'tags': ['pixelart', 'cat'],
    'image': [],
    'description': 'A cat.',
    'nsfw': False,
    'oc': True,
    'ai': '',
    'license': {'type': 'PIXA_LICENSE', 'version': '1.0', 'royaltyPercentage': 0, 'isCustomized': False},
}

BLOCKS: dict[int, list] = {
    1: [account_create('initminer', name) for name in ACCOUNTS],
    2: [
        comment('alice', 'cat', parent_permlink='pixelart', title='Cat', body=ARTWORK, json_metadata=ARTWORK_META),
        comment_options('alice', 'cat', beneficiaries=[('dave', 900)]),
        comment(
            'bob',
            'blog-1',
            parent_permlink='blog',
            title='Hello',
            body='First post',
            json_metadata={'app': 'pixagram', 'format': 'text', 'tags': ['blog']},
        ),
    ],
    3: [
        vote('bob', 'alice', 'cat', 10000),
        effective_vote('bob', 'alice', 'cat', rshares=1000),
        comment('bob', 're-cat', parent_author='alice', parent_permlink='cat', body='Nice!'),
        follow('bob', 'alice', ['blog']),
        follow('carol', 'alice', ['blog']),
        follow('alice', 'bob', ['blog']),
    ],
    4: [
        reblog('carol', 'alice', 'cat'),
        follow('dave', 'alice', ['ignore']),
        transfer('carol', 'null', '1.500', '@alice/cat'),
        transfer('dave', 'null', '2.000', '@bob/blog-1'),
        transfer('dave', 'null', '1.000', '@alice/cat', asset=LIQUID_NAI),  # liquid token: not a promotion
        transfer('dave', 'null', '0.500', 'alice/cat'),  # memo without @: not a promotion
        transfer('carol', 'alice', '1.000', '@alice/cat'),  # not to @null: not a promotion
    ],
    5: [
        comment(
            'alice', 'cat', parent_permlink='pixelart', title='Cat (edited)', body=ARTWORK, json_metadata=ARTWORK_META
        ),
        delete_comment('bob', 're-cat'),
        account_update2('carol', {'profile': {'name': 'Carol', 'version': 2}}),
        author_reward('bob', 'blog-1', '0.100', '0.000', '1.000000'),
    ],
    6: [
        transfer('carol', 'null', '0.250', '@alice/cat'),
    ],
    7: [account_create('initminer', 'portal-100001')],
    8: [
        community_op('alice', 'subscribe', {'community': 'portal-100001'}),
        community_op('bob', 'subscribe', {'community': 'portal-100001', 'action': 'setRole'}),
    ],
}


def load(chain) -> None:
    """Write the scenario's operations into the stub HAF tables."""
    for name in ACCOUNTS:
        chain.add_account(1, name)
    chain.add_account(7, 'portal-100001')
    for block, ops in sorted(BLOCKS.items()):
        chain.add(block, *ops)
