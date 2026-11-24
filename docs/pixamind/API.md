# Pixamind API

JSON-RPC 2.0 over HTTP, as before: nginx rewrites requests to PostgREST, which calls
`hivemind_endpoints.home(json)`. Errors use the inherited error objects
(`-32602` for invalid parameters, with a hived-style `data` block).

```bash
curl -s https://<node> -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"pixamind_api.get_info"}'
```

## Compatibility

Every inherited method keeps its name, parameters and answer shape. Existing clients need
no change.

| Namespace | Methods |
|---|---|
| `bridge` | `get_post`, `get_discussion`, `get_post_header`, `normalize_post`, `get_ranked_posts`, `get_account_posts`, `get_account_posts_by_tag`, `get_profile`, `get_profiles`, `get_relationship_between_accounts`, `does_user_follow_any_lists`, `get_follow_list`, `get_community`, `get_community_context`, `list_communities`, `list_pop_communities`, `list_community_roles`, `list_subscribers`, `list_all_subscriptions`, `account_notifications`, `post_notifications`, `unread_notifications`, `get_payout_stats`, `get_trending_topics`, `list_muted_reasons_enum` |
| `condenser_api` | `get_content`, `get_content_replies`, `get_active_votes`, `get_followers`, `get_following`, `get_follow_count`, `get_reblogged_by`, `get_blog`, `get_blog_entries`, `get_account_reputations`, `get_trending_tags`, `get_discussions_by_created`/`_hot`/`_trending`/`_blog`/`_feed`/`_comments`/`_author_before_date`, `get_post_discussions_by_payout`, `get_comment_discussions_by_payout`, `get_replies_by_last_update`, **`get_discussions_by_promoted`** |
| `follow_api` | `get_followers`, `get_following`, `get_follow_count`, `get_blog`, `get_blog_entries`, `get_reblogged_by`, `get_account_reputations` |
| `tags_api` | `get_discussion`, `get_content_replies`, `get_discussions_by_created`/`_hot`/`_trending`/`_blog`/`_comments`/`_author_before_date`, `get_post_discussions_by_payout`, `get_comment_discussions_by_payout`, **`get_discussions_by_promoted`** |
| `database_api` | `find_comments`, `find_votes`, `list_votes` |
| `hive` | `get_info`, `db_head_state` |
| `search-api` | `find_text` (needs the pg_search extension) |

REST endpoints (PostgREST, OpenAPI described in `hive/db/sql_scripts/endpoints`):
`GET /sync-status`, account operations, pending author/curation rewards, reblogs.

### Deliberate changes

| Change | Why |
|---|---|
| `bridge.get_ranked_posts` accepts `sort: "promoted"`; the "Unsupported sort" message lists it | promoted feed ([PROMOTED_POSTS.md](PROMOTED_POSTS.md)) |
| `condenser_api`/`tags_api.get_discussions_by_promoted` answer again | Steem-era method, same feed |
| Amounts of posts with `comment_options` read `PXS`, not `HBD` | bug fix |
| `rshares` of votes from Pixa's first ~905k blocks are no longer ×10⁶ | bug fix (after a resync) |
| Every post object of the `bridge`, `condenser_api`, `tags_api` and `database_api` methods has `nft_minted` (true or false) | native NFTs, chain hardfork 32 ([NFT.md](NFT.md)) |
| Every post object of the `bridge`, `condenser_api` and `tags_api` methods has `promoted` (`"0.000 PXS"` when nothing was burned for it), as on Steem | promoted posts ([PROMOTED_POSTS.md](PROMOTED_POSTS.md)) |
| `bridge.get_ranked_posts` with `sort: "promoted"` takes `tag: "my"` (the observer's communities) | same scopes as the other sorts |
| On Pixa, every post object names the share of the payout paid in PXS `percent_pxs` (Hivemind: `percent_hbd`) | the name Pixa's node uses ([below](#field-names-on-pixa)) |

`pixamind apidiff` reports exactly these when a Pixamind node is compared with the old one.

### Field names on Pixa

From hardfork 32 code on (pixacore 1.32.0), Pixa's node names the PXS and PIXA fields of its
operations after Pixa: `percent_pxs`, `pxs_payout`, `pixa_payout`, `reward_pixa`, and so on.
HAF writes an operation's JSON with the names of the node code it is built from, so the same
block reads `percent_hbd` through an earlier HAF build and `percent_pxs` through a new one.
The indexer reads both names (`massive_sync.sql` for posts, the events projection for
rewards), so a database synced across the upgrade needs nothing.

What Pixamind answers does not depend on the HAF build:

| Where | Name |
|---|---|
| Post objects of `bridge`, `condenser_api`, `tags_api`, `database_api` | `percent_pxs` with `PIXAMIND_CHAIN=pixa`, `percent_hbd` with `PIXAMIND_CHAIN=hive` (`PIXAMIND_PERCENT_HBD_FIELD` sets it) |
| Column `hivemind_app.hive_posts.percent_hbd` | unchanged (compatibility) |
| `author_reward` events | v1 keys unchanged: `hbd_payout`, `hive_payout` ([EVENTS.md](EVENTS.md)) |

To compare with a node that answers `percent_hbd`, ignore both names at the depths the methods
use, for example `--ignore result.percent_hbd --ignore result.percent_pxs --ignore 'result.*.percent_hbd'
--ignore 'result.*.percent_pxs'`.

## `pixamind_api`

### `get_info` — what this node is

No parameters.

```json
{"name": "pixamind", "version": "0.1.0", "hivemind_base": "1.28.8", "git_revision": "…",
 "chain": {"name": "pixa", "liquid_symbol": "PIXA", "stable_symbol": "PXS", "vests_symbol": "VESTS"},
 "database": {"app_schema": "hivemind_app", "endpoints_schema": "hivemind_endpoints",
              "pixamind_schema": "pixamind", "pixamind_migration": 2,
              "jit": {"api": "off", "database": "off"}},
 "context": {"processed_block": 1234566, "irreversible_block": 1234566, "estimated_head_block": 1234567,
             "stage": "live", "forking": false, "processed_block_age_seconds": 2.1},
 "features": {"events": true, "events_keep_blocks": 864000, "promotions": true, "nfts": true, "telemetry": true,
              "feed_cache": true, "post_history": true, "post_history_backfilled": true},
 "feed_cache": {"enabled": true, "targets": 6, "fresh": 6, "failing": 0, "block": 1234566, "size_bytes": 196608},
 "event_types": ["vote", "post_created", "…"],
 "namespaces": ["pixamind_api", "pixamind_content", "pixamind_graph", "pixamind_feed", "pixamind_nft"]}
```

`database.jit`: `api` is JIT inside API calls (always `off`, see
[OPERATIONS.md](OPERATIONS.md#jit)); `database` is the database-level default, `null` if
the database has none. `feed_cache`: the shared feeds cache
([FEED-CACHE.md](FEED-CACHE.md)) — `fresh` targets are served from the cache at the
current block, `failing` ones could not be computed (the API computes them, and returns
the same error); on a standby it only reports `"standby": true`.

### `get_metrics` — indexer health (for the Systemic Control Tower)

`{"window_minutes": 10}` (1–1440, default 10).

```json
{"context": {"processed_block": 1234566, "irreversible_block": 1234566, "…": "…"},
 "lag": {"blocks_behind_head": 1, "blocks_behind_irreversible": 0, "reversible_blocks_processed": 0},
 "window_minutes": 10,
 "throughput": {"batches": 200, "blocks": 200, "ops": 3100, "busy_seconds": 8.4,
                "blocks_per_second": 0.333, "capacity_blocks_per_second": 23.8, "ops_per_second": 5.167},
 "live_latency_seconds": {"samples": 200, "p50": 1.21, "p95": 2.38, "p99": 3.02, "max": 4.1},
 "phase_seconds_avg": {"load": 0.004, "posts": 0.012, "…": "…"},
 "last_batch": {"mode": "live", "first_block": 1234567, "last_block": 1234567, "…": "…"},
 "events": {"enabled": true, "oldest_block": 370567, "newest_block": 1234567, "estimated_count": 2400000},
 "promoted_posts": 12,
 "failures": {"open": 0, "latest": []}}
```

`live_latency_seconds` is the time from a block's timestamp to Pixamind having indexed
it — the "block to API" delay the proposal asks to minimise. `capacity_blocks_per_second`
is how fast the indexer could go (blocks per busy second); `blocks_per_second` is what it
applied in the window.

### `get_events` — the event outbox

Requires `--pixamind-events`; otherwise error `-32000`. Contract: [EVENTS.md](EVENTS.md).

| Parameter | | |
|---|---|---|
| `cursor` | string | resume after this event id (`next_cursor` of the previous page) |
| `from_block` | int | start at this block when there is no cursor |
| `limit` | int | 1–1000, default 100 |
| `types` | string[] | e.g. `["post_created", "promotion"]` |
| `accounts` | string[] | events whose actor **or** target is one of these (≤ 100) |
| `irreversible_only` | bool | default `true`; only matters on a node whose context is still forking |

```json
{"events": [{"id": "8589934849-0", "block_num": 2, "timestamp": "2026-09-01T00:00:06",
             "type": "post_created", "account": "alice", "target": null, "version": 1,
             "data": {"author": "alice", "permlink": "cat", "category": "pixelart", "title": "Cat",
                      "content": {"format": "image", "app": "pixagram/3.0.2", "tags": ["pixelart", "cat"],
                                  "image": {"mime": "image/webp", "width": 128, "height": 96, "…": "…"}}}}],
 "next_cursor": "8589934849-0", "has_more": false,
 "upto_block": 1234566, "processed_block": 1234566, "irreversible_block": 1234566,
 "retention": {"oldest_block": 370567, "oldest_id": "…", "possible_gap": false}}
```

## `pixamind_content`

### `get_content_metadata`

`{"author": "alice", "permlink": "cat"}`, or `{"posts": [["alice", "cat"], {"author": "bob", "permlink": "x"}]}`
(≤ 100; missing posts are `null` in the batch form, an error in the single form).

```json
{"author": "alice", "permlink": "cat", "depth": 0, "category": "pixelart", "community": "portal-123456",
 "created": "2026-09-01T00:00:06", "updated": "2026-09-01T00:00:15", "title": "Cat",
 "content": {"format": "image", "app": "pixagram/3.0.2", "tags": ["pixelart", "cat"], "description": "A cat.",
             "nsfw": false, "original_content": true,
             "license": {"type": "PIXA_LICENSE", "version": "1.0", "royalty_percentage": 0, "is_customized": false},
             "image": {"mime": "image/webp", "width": 128, "height": 96, "aspect_ratio": 1.3333,
                       "animated": false, "alpha": true, "bytes": 72}}}
```

Read from the post's `json_metadata` and the header of the base64 picture in its body
(PNG, GIF, WebP VP8/VP8L/VP8X): width and height come without decoding the image, so a
masonry feed can reserve space before loading it. `ai_used` appears when the author
declared an AI tool (`json_metadata.ai`).

### `get_post_history`

`{"author": "bob", "permlink": "blog-1", "start": 1, "limit": 20, "bodies": true}` →
every version of a post or comment, oldest first: the creation, then each edit, with
block, timestamp, HAF operation id, the fields that changed, and the full title, body
and json_metadata (`bodies: false` leaves out the bodies). See
[POST-HISTORY.md](POST-HISTORY.md).

```json
{"author": "bob", "permlink": "blog-1", "total": 2, "complete": true,
 "versions": [{"version": 1, "block_num": 2, "timestamp": "…", "op_id": "8589935361", "created": true,
               "changed": ["title", "body", "json_metadata"], "title": "Hello", "body": "First post", "json_metadata": "…"},
              {"version": 2, "block_num": 9, "timestamp": "…", "op_id": "38654705921", "created": false,
               "changed": ["body"], "title": "Hello", "body": "Final post", "json_metadata": "…"}]}
```

## `pixamind_graph`

### `get_mutual_follows`

`{"account": "alice", "start": "", "limit": 100}` → accounts alice follows that follow
her back, by name: `["bob"]`.

### `get_relationships`

`{"observer": "dave", "accounts": ["alice", "bob"]}` (≤ 100) → one call for a page of cards:

```json
{"alice": {"follows": false, "followed_by": false, "ignores": true, "blacklists": false,
           "follows_blacklists": false, "follows_muted": false},
 "bob":   {"follows": false, "followed_by": false, "ignores": false, "…": false}}
```

## `pixamind_feed`

### `get_promoted_posts`

Same parameters as `bridge.get_ranked_posts` without `sort` (`tag`, `observer`, `limit`,
`start_author`, `start_permlink`, `muted_reasons_filter`). Bridge post objects, highest
promotion first, each with `"promoted": "2.000 PXS"`.

### `get_promotions`

`{"author": "alice", "permlink": "cat"}` → the payments behind a post:

```json
{"author": "alice", "permlink": "cat", "promoted": "1.750 PXS",
 "promotions": [{"sender": "carol", "amount": "0.250 PXS", "block_num": 6, "timestamp": "…"},
                {"sender": "carol", "amount": "1.500 PXS", "block_num": 4, "timestamp": "…"}]}
```

`{"sender": "dave", "limit": 100}` → what an account spent on promotion, newest first.

## `pixamind_nft`

Native NFTs (chain hardfork 32): `get_nfts`, `list_editions`, `list_created`, `list_owned`,
`list_market`, `get_history`, `get_license`. An NFT is addressed as `@author/permlink/0` and its
copy N (1 to 10,000) as `@author/permlink/N`. Parameters, answers and examples: [NFT.md](NFT.md).
