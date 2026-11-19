# Event outbox — contract v1

The outbox turns the social operations of each block into a stream that other services
read incrementally: Pixagram Search (index new and edited artworks without polling
feeds), the Systemic Control Tower (activity metrics), notification or analytics
services. It is the transactional outbox of the proposal's §6, kept in PostgreSQL.

Enable it on the indexer with `--pixamind-events` (`PIXAMIND_EVENTS=true`). Read it with
`pixamind_api.get_events` ([API.md](API.md)) or `pixamind events tail`.

## What an event is

A normalised social operation from a block, after hivemind has processed that block.
Events describe what happened on chain, checked the way hivemind checks it (signer,
payload shape); they are not hivemind's row changes. When an event says less than you
need, ask the API (e.g. `bridge.get_post`) for the current state.

```json
{"id": "8589934849-0", "block_num": 2, "timestamp": "2026-09-01T00:00:06",
 "type": "post_created", "account": "alice", "target": null, "version": 1, "data": {…}}
```

| Field | |
|---|---|
| `id` | `"<op_id>-<seq>"`: the HAF operation id (block number and position in the block) and the index of the event within that operation. A string because it exceeds 2⁵³. Identical on every node, after every resync. |
| `block_num`, `timestamp` | the block and its timestamp (UTC) |
| `account` | who acted (voter, author, follower, sender, …) |
| `target` | who is affected (post author, followed account, …), or null |
| `version` | payload version, `1` |

## Types (v1)

| `type` | `account` → `target` | `data` |
|---|---|---|
| `vote` | voter → author | `voter`, `author`, `permlink`, `weight` (−10000…10000) |
| `post_created`, `post_updated` | author | `author`, `permlink`, `category`, `title`, `content` |
| `comment_created`, `comment_updated` | author → parent author | `author`, `permlink`, `parent_author`, `parent_permlink`, `title`, `content` |
| `post_deleted` | author | `author`, `permlink` (only deletes the chain accepted) |
| `follow` | follower → following | `follower`, `following`, `action`: `follow`, `unfollow`, `mute`, `blacklist`, `unblacklist`, `follow_blacklist`, `unfollow_blacklist`, `follow_muted`, `unfollow_muted`, or a `reset_*` action (no `following`) |
| `reblog`, `reblog_deleted` | account → author | `account`, `author`, `permlink` |
| `community` | signer → `params.account` | `action` (`subscribe`, `unsubscribe`, `setRole`, `setUserTitle`, `mutePost`, `unmutePost`, `pinPost`, `unpinPost`, `flagPost`, `updateProps`), `community`, the operation's other parameters. Permissions are enforced by hivemind; this is the request as sent. |
| `account_created` | new account → creator | `account`, `creator` |
| `account_updated` | account | `account`, `metadata`, `posting_metadata` (which of the two changed) |
| `author_reward` | author | `author`, `permlink`, `hbd_payout`, `hive_payout`, `vesting_payout` ("0.100 PXS") |
| `promotion` | sender → author | `sender`, `author`, `permlink`, `amount`, `post_exists` |

`content` is the normalised metadata of [API.md § pixamind_content](API.md#pixamind_content)
(format, app, tags, description, NSFW flag, original-content flag, AI disclosure, licence,
and for pictures mime type, width, height, animation, alpha, size). Created vs. updated:
the first operation for a permlink in the block where hivemind created the post is the
creation; anything else is an update. Edits carry a diff, so their `content.image` is
usually absent.

## Delivery guarantees

* **Order.** Ids follow chain order. Page with `next_cursor`; you never skip an event.
* **Exactly the chain.** Every batch deletes and rewrites its own block range. A replayed
  batch (crash recovery) produces the same rows; re-indexing gives the same ids.
* **Irreversible by default.** Pixa's indexer follows reversible blocks. `get_events`
  only returns blocks up to `min(processed, last irreversible)`, so what you read can
  never be undone. With `irreversible_only: false` you see newer blocks too, but an event
  can then disappear in a micro-fork and its replacement may sort before your cursor: use
  it only for best-effort live UI.
* **Atomic with the social state.** In live sync a block's events commit with the block;
  when you see an event, the API already reflects it.
* **Retention.** Events older than `--pixamind-events-retention-days` (default 30) are
  pruned hourly; a full sync writes only that window. `retention.possible_gap` tells a
  consumer whose cursor fell behind the oldest kept event; rebuild older ranges from the
  chain with `pixamind events backfill`.
* **Failures.** If deriving events fails for a block range the indexer carries on, records
  the range in `pixamind.projection_failures` (visible in `get_metrics`), and
  `pixamind events backfill --from-block A --to-block B` repairs it.

## Cost

Measured with the SQL harness on a small machine: a 1000-block batch with 40 social
operations per block (40,000 events) takes about 1 s to write, i.e. ~25 µs per event; a
live block takes a millisecond or two. During a full sync only the retention window is
written. Size on disk is roughly that of the operations' JSON, minus post bodies.

## Consumer sketch (Cloudflare Worker / Node)

```js
let cursor = await kv.get('pixamind-cursor');            // null on first run
const body = {jsonrpc: '2.0', id: 1, method: 'pixamind_api.get_events',
              params: {limit: 500, types: ['post_created', 'post_updated', 'post_deleted'],
                       ...(cursor ? {cursor} : {from_block: 1})}};
const {result} = await (await fetch(API, {method: 'POST', body: JSON.stringify(body)})).json();
for (const e of result.events) await indexArtwork(e);    // idempotent upsert by author/permlink
if (result.next_cursor) await kv.put('pixamind-cursor', result.next_cursor);
// result.has_more: call again right away; otherwise wait a block (3 s)
```

## Versioning

New event types may be added within v1; consumers must ignore types they do not know.
Changing the meaning or shape of an existing type's `data` bumps `version` (both versions
are then written during a transition period).
