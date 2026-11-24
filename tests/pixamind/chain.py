"""Synthetic chain operations, in the JSON shape HAF stores (`body_value`).

Used with the SQL harness: ops are appended to the stub hafd.operations table,
then the real hivemind block processor indexes them (see livesync.py).
"""

from __future__ import annotations

import json
from typing import Any

# HAF operation type ids
VOTE = 0
COMMENT = 1
TRANSFER = 2
ACCOUNT_CREATE = 9
ACCOUNT_UPDATE = 10
DELETE_COMMENT = 17
CUSTOM_JSON = 18
COMMENT_OPTIONS = 19
ACCOUNT_UPDATE2 = 43
NFT = 37  # nft_operation (hardfork 32); the number of the former reset_account_operation placeholder
AUTHOR_REWARD = 51
EFFECTIVE_COMMENT_VOTE = 72
INEFFECTIVE_DELETE_COMMENT = 73
NFT_SOLD = 93
NFT_ROYALTY = 94

STABLE_NAI = '@@000000013'
LIQUID_NAI = '@@000000021'
VESTS_NAI = '@@000000037'

_AUTHORITY = {'weight_threshold': 1, 'account_auths': [], 'key_auths': [['STM' + 'x' * 50, 1]]}


def nai(amount: str | float, asset: str = STABLE_NAI) -> dict[str, Any]:
    precision = 6 if asset == VESTS_NAI else 3
    return {'amount': str(round(float(amount) * 10**precision)), 'precision': precision, 'nai': asset}


def account_create(creator: str, name: str) -> tuple[int, dict]:
    return ACCOUNT_CREATE, {
        'fee': nai('3.000', LIQUID_NAI),
        'creator': creator,
        'new_account_name': name,
        'owner': _AUTHORITY,
        'active': _AUTHORITY,
        'posting': _AUTHORITY,
        'memo_key': 'STM' + 'x' * 50,
        'json_metadata': '',
    }


def comment(
    author: str,
    permlink: str,
    *,
    parent_author: str = '',
    parent_permlink: str = 'pixelart',
    title: str = '',
    body: str = 'hello',
    json_metadata: dict | str | None = None,
) -> tuple[int, dict]:
    if isinstance(json_metadata, dict):
        json_metadata = json.dumps(json_metadata)
    return COMMENT, {
        'parent_author': parent_author,
        'parent_permlink': parent_permlink,
        'author': author,
        'permlink': permlink,
        'title': title,
        'body': body,
        'json_metadata': json_metadata or '',
    }


def comment_options(
    author: str,
    permlink: str,
    *,
    max_accepted_payout: str = '1000000.000',
    beneficiaries: list[tuple[str, int]] | None = None,
) -> tuple[int, dict]:
    extensions = []
    if beneficiaries:
        extensions.append(
            {
                'type': 'comment_payout_beneficiaries',
                'value': {'beneficiaries': [{'account': a, 'weight': w} for a, w in beneficiaries]},
            }
        )
    return COMMENT_OPTIONS, {
        'author': author,
        'permlink': permlink,
        'max_accepted_payout': nai(max_accepted_payout),
        'percent_hbd': 10000,
        'allow_votes': True,
        'allow_curation_rewards': True,
        'extensions': extensions,
    }


def vote(voter: str, author: str, permlink: str, weight: int = 10000) -> tuple[int, dict]:
    return VOTE, {'voter': voter, 'author': author, 'permlink': permlink, 'weight': weight}


def effective_vote(voter: str, author: str, permlink: str, rshares: int, weight: int = 1000) -> tuple[int, dict]:
    return EFFECTIVE_COMMENT_VOTE, {
        'voter': voter,
        'author': author,
        'permlink': permlink,
        'weight': weight,
        'rshares': rshares,
        'total_vote_weight': weight,
        'pending_payout': nai('0.010'),
    }


def custom_json(cj_id: str, signer: str, payload: Any, *, active: bool = False) -> tuple[int, dict]:
    return CUSTOM_JSON, {
        'required_auths': [signer] if active else [],
        'required_posting_auths': [] if active else [signer],
        'id': cj_id,
        'json': json.dumps(payload),
    }


def follow(follower: str, following: str | list[str], what: list[str]) -> tuple[int, dict]:
    return custom_json('follow', follower, ['follow', {'follower': follower, 'following': following, 'what': what}])


def reblog(account: str, author: str, permlink: str, delete: bool = False) -> tuple[int, dict]:
    body = {'account': account, 'author': author, 'permlink': permlink}
    if delete:
        body['delete'] = 'delete'
    return custom_json('follow', account, ['reblog', body])


def community_op(signer: str, action: str, params: dict) -> tuple[int, dict]:
    return custom_json('community', signer, [action, params])


def transfer(sender: str, to: str, amount: str, memo: str = '', asset: str = STABLE_NAI) -> tuple[int, dict]:
    return TRANSFER, {'from': sender, 'to': to, 'amount': nai(amount, asset), 'memo': memo}


def delete_comment(author: str, permlink: str) -> tuple[int, dict]:
    return DELETE_COMMENT, {'author': author, 'permlink': permlink}


def account_update2(account: str, posting_json_metadata: dict | None = None) -> tuple[int, dict]:
    return ACCOUNT_UPDATE2, {
        'account': account,
        'json_metadata': '',
        'posting_json_metadata': json.dumps(posting_json_metadata or {}),
        'extensions': [],
    }


def author_reward(author: str, permlink: str, hbd: str, hive: str, vests: str) -> tuple[int, dict]:
    return AUTHOR_REWARD, {
        'author': author,
        'permlink': permlink,
        'hbd_payout': nai(hbd, STABLE_NAI),
        'hive_payout': nai(hive, LIQUID_NAI),
        'vesting_payout': nai(vests, VESTS_NAI),
        'curators_vesting_payout': nai('0', VESTS_NAI),
        'payout_must_be_claimed': True,
    }


# ----------------------------------------------------------------- NFTs
# nft_operation carries one action; a purchase is followed by the virtual operations
# nft_sold (always) and nft_royalty (when the creator is paid a royalty).
NFT_NEVER = '2106-02-07T06:28:15'  # time_point_sec::maximum(): an offer that does not expire


def _nft(account: str, action: str, **value: Any) -> tuple[int, dict]:
    return NFT, {
        'account': account,
        'action': {'type': f'nft_{action}', 'value': {**value, 'extensions': []}},
        'extensions': [],
    }


def nft_mint(
    account: str, permlink: str, royalty: int = 0, license: str = '', editions: int = 1, fee: str | None = None
) -> tuple[int, dict]:
    """The fee is 1.000 PXS per copy unless given."""
    return _nft(
        account,
        'mint',
        permlink=permlink,
        editions=editions,
        royalty=royalty,
        license=license,
        fee=nai(fee if fee is not None else f'{editions}.000', STABLE_NAI),
    )


def nft_list(
    account: str, author: str, permlink: str, price: str, expiration: str = NFT_NEVER, edition: int = 1
) -> tuple[int, dict]:
    """An offer, in PXS."""
    return _nft(
        account,
        'list',
        author=author,
        permlink=permlink,
        edition=edition,
        price=nai(price, STABLE_NAI),
        expiration=expiration,
    )


def nft_unlist(account: str, author: str, permlink: str, edition: int = 1) -> tuple[int, dict]:
    return _nft(account, 'unlist', author=author, permlink=permlink, edition=edition)


def nft_buy(account: str, author: str, permlink: str, price: str, edition: int = 1) -> tuple[int, dict]:
    return _nft(account, 'buy', author=author, permlink=permlink, edition=edition, price=nai(price, STABLE_NAI))


def nft_transfer(account: str, author: str, permlink: str, to: str, edition: int = 1) -> tuple[int, dict]:
    """A gift, without payment or royalty; to the null account it burns the copy."""
    return _nft(account, 'transfer', author=author, permlink=permlink, edition=edition, to=to)


def nft_sold(
    seller: str, buyer: str, author: str, permlink: str, price: str, royalty: str = '0.000', edition: int = 1
) -> tuple[int, dict]:
    """Price and royalty in PXS."""
    return NFT_SOLD, {
        'seller': seller,
        'buyer': buyer,
        'author': author,
        'permlink': permlink,
        'edition': edition,
        'price': nai(price, STABLE_NAI),
        'royalty': nai(royalty, STABLE_NAI),
    }


def nft_royalty(
    creator: str, seller: str, buyer: str, permlink: str, royalty: str, edition: int = 1
) -> tuple[int, dict]:
    return NFT_ROYALTY, {
        'creator': creator,
        'seller': seller,
        'buyer': buyer,
        'permlink': permlink,
        'edition': edition,
        'royalty': nai(royalty, STABLE_NAI),
    }


# --------------------------------------------------------------- images
# Smallest valid headers for the image formats Pixagram posts carry. Only the
# header matters to pixamind.image_info; the rest is padding.


def webp_vp8x_b64(width: int, height: int, *, animated: bool = False, alpha: bool = True) -> str:
    import base64
    import struct

    flags = (0x02 if animated else 0) | (0x10 if alpha else 0)
    chunk = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
    body = b'WEBP' + b'VP8X' + struct.pack('<I', len(chunk)) + chunk + b'\x00' * 40
    return base64.b64encode(b'RIFF' + struct.pack('<I', len(body)) + body).decode()


def webp_vp8l_b64(width: int, height: int, *, alpha: bool = False) -> str:
    import base64
    import struct

    bits = (width - 1) | ((height - 1) << 14) | ((1 if alpha else 0) << 28)
    chunk = b'\x2f' + struct.pack('<I', bits) + b'\x00' * 40
    body = b'WEBP' + b'VP8L' + struct.pack('<I', len(chunk)) + chunk
    return base64.b64encode(b'RIFF' + struct.pack('<I', len(body)) + body).decode()


def webp_vp8_b64(width: int, height: int) -> str:
    import base64
    import struct

    frame = b'\x9d\x01\x2a' + struct.pack('<HH', width, height)
    chunk = b'\x00\x00\x00' + frame + b'\x00' * 40
    body = b'WEBP' + b'VP8 ' + struct.pack('<I', len(chunk)) + chunk
    return base64.b64encode(b'RIFF' + struct.pack('<I', len(body)) + body).decode()


def png_b64(width: int, height: int, color_type: int = 6, *, apng: bool = False) -> str:
    import base64
    import struct
    import zlib

    ihdr = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
    data = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + ihdr + struct.pack('>I', zlib.crc32(b'IHDR' + ihdr))
    if apng:
        actl = struct.pack('>II', 2, 0)
        data += struct.pack('>I', 8) + b'acTL' + actl + struct.pack('>I', zlib.crc32(b'acTL' + actl))
    return base64.b64encode(data + b'\x00' * 40).decode()


def gif_b64(width: int, height: int) -> str:
    import base64
    import struct

    return base64.b64encode(b'GIF89a' + struct.pack('<HH', width, height) + b'\x00' * 50).decode()
