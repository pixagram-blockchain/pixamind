# NFTs

Pixa has native NFTs since chain hardfork 32: a post is minted with 1 to 10,000 copies, and
each copy is offered, bought, given away or burned with one chain operation,
`nft_operation` (id 37). Pixamind indexes those operations and serves what a marketplace
needs on top of the node: the history of each NFT, its market summary, its copies page by
page, and the lists by creator, by owner and by price.

An NFT is named `@author/permlink/0`; its copy N (1 to 10,000) is `@author/permlink/N`.
Prices, royalties and the mint fee are in PXS: minting costs 1 PXS per copy, burned. A copy
given to `null` is burned: it stays among the copies of its NFT, owned by `null`. The license
is the full text of the terms, up to 72 KiB; the chain keeps its SHA-256, and Pixamind keeps
the text as well so that `get_license` can return it.

## Copies nothing happened to

On chain a copy gets state of its own only when it is first offered or first leaves its
creator; until then it is the creator's, acquired at mint, not offered. Pixamind follows the
same rule: `pixamind.nft_editions` has a row for a copy once an event concerns it, and every
method answers for the other copies all the same (`pixamind.nft_untouched_edition`). A mint
of 10,000 copies therefore adds one row to `pixamind.nfts` and none to `nft_editions`, and a
client never has to know whether a copy has a row.

## The reserve

The copies without a row are the creator's **reserve**: they never left the creator and have
no offer of their own. The creator can offer the whole reserve at one price in one action, an
`nft_list` of copy 0 (`@author/permlink/0`); a buyer sends `nft_buy` of copy 0 at that price
and gets the lowest-numbered copy still in the reserve, which the chain names in the
`nft_sold_operation` of the sale. All of the price goes to the creator. A copy the creator
offers on its own leaves the reserve; when the creator withdraws that offer, the copy goes back
(the chain removes its state, Pixamind its row). The reserve offer ends by itself when the last
copy leaves the reserve, and stays ended if a copy comes back later.

Pixamind keeps the reserve on the NFT's row: `reserve` (how many copies) and the offer
(`reserve_price`, `reserve_expiration`, `reserve_listed_op_id`, `reserve_listed_at`), rebuilt
with the copies an event touches.

## Market summary: what an NFT page needs in one call

`get_nfts` with `@author/permlink/0` (or `get_content_metadata`, under `nft`) answers:

| Question | Field |
|---|---|
| How many copies? | `copies` (the same as `editions`) |
| How many on sale? | `for_sale`: every copy offered on its own plus the whole reserve while it is offered; `listed` counts the first kind only |
| Lowest price? | `floor.price` |
| How many at that price, and which ids to buy them with? | `floor.copies` and `floor.nft_ids` (at most 100, by number). `@author/permlink/0` in that list is the reserve: buying it gives the copy named in `reserve_offer.next` |
| How many in the reserve? | `reserve` |
| Is the reserve offered, at what price, which copy comes next? | `reserve_offer`: `price`, `copies`, `next`, `expiration` when it has one, `listed_at`; absent when the reserve cannot be bought now |
| How many does the creator still hold? | `held_by_creator` (the reserve, plus copies the creator offered on its own or got back) |

## What is indexed

| Chain | Pixamind |
|---|---|
| `nft_operation` with action `nft_mint`, `nft_list`, `nft_unlist`, `nft_transfer` | one row in `pixamind.nft_events`; a transfer to `null` is a `burn`; a mint keeps the license text and its SHA-256; `nft_list` and `nft_unlist` of copy 0 are the reserve offer, edition 0 |
| `nft_sold_operation` (virtual, id 93), produced by every `nft_buy` | one `sale` row, with seller, price and royalty, against the copy it names (for a purchase of copy 0, the copy the reserve gave) |
| `nft_operation` with action `nft_buy` | not recorded: its `nft_sold_operation` says the same and more |
| `nft_royalty_operation` (virtual, id 94) | not read: the sale row already holds the royalty |

`pixamind.process_nfts(first, last)` (runtime `55_nft.sql`) runs in `pixamind.after_batch`.
It deletes and rewrites the events of its block range, then rebuilds what they touch: the
row in `pixamind.nfts` of an NFT minted in the range, the row in `pixamind.nft_editions`
of each copy an event in the range concerns, from that copy's whole history, and the reserve
of each NFT the range touched. The reserve offer is the last offer of copy 0 unless it was
withdrawn or the reserve ran out after it, which `pixamind.nft_reserve_offer_state` checks by
following the number of copies with state event by event. The tables are
therefore the same whatever the batch boundaries were and after any replay, and a busy NFT
costs no more per block than the copies that moved in it. Everything in a block was
accepted by consensus; nothing is validated again.

Switch: `--pixamind-nfts` / `PIXAMIND_NFTS` (default on).

## `nft_minted` on every post

Every post that the bridge, condenser and database APIs return carries `"nft_minted": true`
or `false` (`bridge.get_post`, `bridge.get_ranked_posts`, `condenser_api.get_content`,
`database_api.find_comments` and the others built on the same three post builders). With
`--pixamind-nfts` off nothing is indexed and every post says `false`.

When `pixamind apidiff` compares with a node that predates this field, ignore it at the depths
the methods use: `--ignore result.nft_minted --ignore 'result.*.nft_minted'
--ignore 'result.comments.*.nft_minted'`. The inherited tavern patterns
(`tests/api_tests/hivemind/tavern`, GitLab CI only) do not have the field yet.

## API: `pixamind_nft`

| Method | Parameters | Result |
|---|---|---|
| `get_nfts` | `{"nft": id}` or `{"nfts": [id, …]}`, at most 100 ids | `{"nfts": […]}`: for `…/0` the NFT with its terms and market summary, for `…/N` that copy with the terms of its NFT under `nft`. Ids that name nothing are left out; each id is answered once, in the order first asked |
| `list_editions` | `{"nft": id, "sort": "number" \| "price", "owner", "limit", "start"}` | the copies of one NFT: `number` lists every copy in order, those of the reserve included with `"reserve": true`; `price` lists what can be bought now, cheapest first, the offered reserve as one entry, copy 0, with `copies` and `next`; `owner` keeps one account's copies |
| `list_created` | `{"account", "limit", "start"}` | NFTs the account minted, newest first, each with its summary, and `total` |
| `list_owned` | `{"account", "acquired_only", "limit", "start"}` | one entry per NFT the account holds copies of, latest acquisition first: the terms under `nft`, `held`, `listed` (of those held, how many can be bought now) and `acquired`; a creator holds the copies it never parted with, which `acquired_only` leaves out |
| `list_market` | `{"sort": "recent" \| "price", "creator", "limit", "start"}` | what can be bought now, each with the terms of its NFT: copies offered on their own, and offered reserves as one entry each (copy 0, `"reserve": true`, `copies`, `next`) |
| `get_history` | `{"nft": id}` or `{"account"}`, plus `limit`, `start` | newest first: mint, offers, sales, gifts, burns; `…/0` covers every copy, `…/N` one copy and the mint |
| `get_license` | `{"nft": id}` | the full license text with `license_size` and `license_sha256`; `{}` when the post is not minted |

Lists return `next_start` and `has_more`; pass `next_start` back as `start` and treat it as
opaque. Lists take at most 100 entries per call (`get_history` 1,000). "For sale" compares
the offer's expiration with `hivemind_app.head_block_time()`, never `now()`. A value that
does not apply is left out of an object rather than set to `null`. Objects describe the
license by `license_size` and `license_sha256` only; the text is for `get_license`.

`get_nfts` with `{"nft": "@carol/drop/0"}`, an NFT with 5 copies: carol offered her reserve at
5 PXS, three buyers took copies 1, 3 and 2 from it, and bob offers his copy 1 at the same price
(real output of the SQL test, keys reordered):

```json
{"nfts": [{
  "nft_id": "@carol/drop/0", "author": "carol", "permlink": "drop",
  "minted": "2027-01-10T12:02:03", "mint_block": 41, "editions": 5,
  "royalty": 1000, "royalty_percent": "10.00", "mint_fee": "5.000 PXS",
  "license_size": 1,
  "license_sha256": "72dfcfb0c470ac255cde83fb8fe38de8a128188e03ea5ba5b2a93adbea1062fa",
  "post_exists": true,
  "copies": 5, "for_sale": 3, "listed": 1, "reserve": 2, "burned": 0, "held_by_creator": 2,
  "reserve_offer": {"nft_id": "@carol/drop/0", "author": "carol", "permlink": "drop", "edition": 0,
                    "owner": "carol", "reserve": true, "copies": 2, "next": "@carol/drop/4",
                    "listed": true, "price": "5.000 PXS", "listed_at": "2027-01-10T12:02:09"},
  "floor": {"price": "5.000 PXS", "copies": 3, "nft_ids": ["@carol/drop/0", "@carol/drop/1"]},
  "sales": 3,
  "last_sale": {"price": "5.000 PXS", "edition": 2, "time": "2027-01-10T12:02:21"},
  "volume": "15.000 PXS", "royalties": "0.000 PXS"
}]}
```

The same call for `@alice/series/0`, 10,000 copies with four offered on their own and no
reserve offer: `"copies": 10000, "for_sale": 4, "listed": 4, "reserve": 9994,
"floor": {"price": "260.000 PXS", "copies": 1, "nft_ids": ["@alice/series/4"]}`.

`floor` and `last_sale` are left out when there is no offer or no sale, `reserve_offer` when
the reserve cannot be bought now. `pixamind_content.get_content_metadata` carries the same summary under `nft`, for minted
posts only.

`list_editions` with `{"nft": "@alice/series/0", "limit": 3, "start": "4"}`: copy 4 is
offered, copy 5 is burned, nothing ever happened to copy 6 (it is in the reserve):

```json
{"nft_id": "@alice/series/0", "sort": "number",
 "editions": [
  {"nft_id": "@alice/series/4", "author": "alice", "permlink": "series", "edition": 4,
   "owner": "alice", "burned": false, "acquired": "2027-01-10T12:01:33", "listed": true,
   "price": "260.000 PXS", "listed_at": "2027-01-10T12:01:33",
   "sales": 0, "volume": "0.000 PXS", "royalties": "0.000 PXS"},
  {"nft_id": "@alice/series/5", "author": "alice", "permlink": "series", "edition": 5,
   "owner": "null", "burned": true, "acquired": "2027-01-10T12:01:39", "listed": false,
   "sales": 0, "volume": "0.000 PXS", "royalties": "0.000 PXS"},
  {"nft_id": "@alice/series/6", "author": "alice", "permlink": "series", "edition": 6,
   "owner": "alice", "burned": false, "acquired": "2027-01-10T12:01:33", "listed": false,
   "sales": 0, "volume": "0.000 PXS", "royalties": "0.000 PXS", "reserve": true}],
 "next_start": "7", "has_more": true}
```

`list_owned` with `{"account": "alice", "limit": 1}` (the terms under `nft` cut):

```json
{"account": "alice",
 "nfts": [{"nft": {"nft_id": "@alice/series/0", "…": "…"}, "held": 9997, "listed": 3,
           "acquired": "2027-01-10T12:01:33"}],
 "next_start": "133143986213", "has_more": true}
```

Two entries of `get_history`: the mint, then the resale of copy 1:

```json
{"id": "133143986213", "block_num": 31, "timestamp": "2027-01-10T12:01:33", "type": "mint",
 "nft_id": "@alice/series/0", "author": "alice", "permlink": "series", "account": "alice",
 "editions": 10000, "royalty_rate": 1000, "fee": "10000.000 PXS", "license_size": 73728,
 "license_sha256": "f7aeea5b061bf61fd88f9228dc625692d4413c7672b1ab7098c143452a405b7a"}
{"id": "150323855709", "block_num": 35, "timestamp": "2027-01-10T12:01:45", "type": "sale",
 "nft_id": "@alice/series/1", "author": "alice", "permlink": "series", "edition": 1,
 "account": "dave", "seller": "eve", "price": "300.000 PXS", "royalty": "30.000 PXS"}
```

`get_license` with `{"nft": "@alice/art-two/0"}`:

```json
{"nft_id": "@alice/art-two/0", "license": "Licence « tous droits réservés »",
 "license_size": 36,
 "license_sha256": "60f3ce9a6d0a3d68870971e61813a4a969002dad70a0fd062e6a2d3e3d853471"}
```

`license_sha256` is the SHA-256 of the UTF-8 bytes of the text, as the node computes it at
mint and returns it from `database_api.find_nfts`: comparing the two proves the text is the
one the chain recorded.

## Events

With the event outbox on, `pixamind_api.get_events` also returns these types. Every
payload starts with `nft_id`.

| Event | `account` | `target` | Payload |
|---|---|---|---|
| `nft_minted` | minter | none | `author`, `permlink`, `editions`, `royalty`, `license_size`, `license_sha256`, `fee` |
| `nft_listed` | owner | creator | `account`, `author`, `permlink`, `edition`, `price`, and `expiration` when the offer has one; edition 0 (`…/0`) is the creator offering the reserve |
| `nft_unlisted` | owner | creator | `account`, `author`, `permlink`, `edition`; edition 0 withdraws the reserve offer |
| `nft_sold` | buyer | seller | `buyer`, `seller`, `author`, `permlink`, `edition`, `price`, `royalty`; a sale from the reserve names the copy it gave |
| `nft_transferred` | giver | receiver | `from`, `to`, `author`, `permlink`, `edition` |
| `nft_burned` | owner who burned it | creator | `account`, `author`, `permlink`, `edition` |

## Operations

The chain rejects edits and deletion of a minted post, so the post tables never change
under an NFT. Fingerprints (`pixamind integrity`) cover `nft_events`, `nfts` and
`nft_editions`. A failed batch is recorded in `pixamind.projection_failures` under
`nfts` and never stops the indexer; reprocessing the range resolves it.
