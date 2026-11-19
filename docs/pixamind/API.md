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

`pixamind apidiff` reports exactly these when a Pixamind node is compared with the old one.

## `pixamind_api`

### `get_info` — what this node is

No parameters.

```json
{"name": "pixamind", "version": "0.1.0", "hivemind_base": "1.28.8", "git_revision": "…",
 "chain": {"name": "pixa", "liquid_symbol": "PIXA", "stable_symbol": "PXS", "vests_symbol": "VESTS"},
 "database": {"app_schema": "hivemind_app", "endpoints_schema": "hivemind_endpoints",
              "pixamind_schema": "pixamind", "pixamind_migration": 1},
 "context": {"processed_block": 1234567, "irreversible_block": 1234566, "estimated_head_block": 1234568,
             "stage": "live", "forking": true, "processed_block_age_seconds": 2.1},
 "features": {"events": true, "events_keep_blocks": 864000, "promotions": true, "telemetry": true},
 "event_types": ["vote", "post_created", "…"],
 "namespaces": ["pixamind_api", "pixamind_content", "pixamind_graph", "pixamind_feed"]}
```

### `get_metrics` — indexer health (for the Systemic Control Tower)

`{"window_minutes": 10}` (1–1440, default 10).

```json
{"context": {"processed_block": 1234567, "irreversible_block": 1234566, "…": "…"},
 "lag": {"blocks_behind_head": 1, "blocks_behind_irreversible": 0, "reversible_blocks_processed": 1},
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
| `irreversible_only` | bool | default `true` |

```json
{"events": [{"id": "8589934849-0", "block_num": 2, "timestamp": "2026-09-01T00:00:06",
             "type": "post_created", "account": "alice", "target": null, "version": 1,
             "data": {"author": "alice", "permlink": "cat", "category": "pixelart", "title": "Cat",
                      "content": {"format": "image", "app": "pixagram/3.0.2", "tags": ["pixelart", "cat"],
                                  "image": {"mime": "image/webp", "width": 128, "height": 96, "…": "…"}}}}],
 "next_cursor": "8589934849-0", "has_more": false,
 "upto_block": 1234566, "processed_block": 1234567, "irreversible_block": 1234566,
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
