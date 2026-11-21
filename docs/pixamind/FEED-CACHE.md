# Shared feeds cache

Most feed requests have no observer, so every user gets the same answer: the Pixagram
front page asks `bridge.get_ranked_posts` for the same 20 trending posts for every
visitor, and hivemind rebuilds those 20 posts (votes, payouts, authors, communities)
each time, although the answer can only change when a block is indexed. Pixamind
computes each such feed **once per block** and answers the requests from a table: a
primary-key lookup instead of the feed query.

It is on by default (`--pixamind-feed-cache` / `PIXAMIND_FEED_CACHE`), needs no extra
service, and clients notice nothing but the speed: the bytes are the same.

## How it works

```
live block N:  hivemind processes N ── COMMIT ── refresh_feed_cache() ── COMMIT
                                         │                │
API:           computes the feeds ───────┘ (rows of N-1   └── serves the feeds from
               no longer match block N)                       pixamind.feed_cache
```

1. **Targets.** `pixamind.feed_cache_targets` lists the requests to keep warm: `api`,
   `method`, `params`. It starts with what the Pixagram web app sends (below).
2. **Refresh.** Right after a live block commits, the indexer runs
   `pixamind.refresh_feed_cache()` in a transaction of its own. Each target is computed
   with the same `hivemind_postgrest_utilities.dispatch()` the API uses, and stored with
   the block the `hivemind_app` context is at. A result identical to the stored one
   (same md5) is not rewritten; the row just moves to the new block. The refresh runs
   after the block's COMMIT, so it never delays the block itself.
3. **Serving.** `hivemind_endpoints.home` asks
   `hivemind_postgrest_utilities.cached_result(api, method, params)` before dispatching.
   It returns the stored result only when
   * `api`, `method` and `params` match a row (the same `jsonb`: key order, spacing and
     the request `id` do not matter; number spelling does, `20` is not `20.0`),
   * the context is still at the row's block,
   * the session's `hivemind_app.max_posts_per_call` is the one the row was computed
     with,
   * the row holds a result (failed requests are always computed, so errors are the
     API's own), and
   * the server is not a standby (the table is unlogged, see below).

   Otherwise the request is computed as before. The lookup costs a few microseconds.
4. **Envelope.** The stored result is the text `dispatch()` returned, and `home()`
   wraps it exactly as it wraps a computed one, so the response is byte-identical
   (`tests/pixamind/test_feed_cache.py` checks it for every target, through
   `home()`, as the API roles, in read-only transactions like PostgREST).

Between a block's COMMIT and the refresh's COMMIT (milliseconds), the rows still belong
to the previous block, so the API computes those feeds itself: nothing older than the
current block is ever served.

**What can differ.** Everything hivemind indexes is exactly what the API would compute.
The one input from another HAF application, the authors' reputation (the reputation
tracker runs its own sync), is the one it had when the block's feeds were computed — at
most one block earlier than a computed answer would show.

## When it does not refresh

* **Massive sync.** Its batches commit in several steps and are followed by the
  finalization; the cache stays empty until the first live block.
* **Catching up.** With more than 20 irreversible blocks waiting, the next block follows
  within milliseconds and nobody would be served the result.
* **Start.** The indexer empties the cache when it starts (a previous run may have
  ended before a context rewind, a resync or hand edits), and when the cache is off.
* **Failures.** A target that fails (for example invalid params) is stored without a
  result and computed by the API, which returns the same error. If the whole refresh
  fails, the block is not affected; the rows stay on the previous block and are not
  served.

## The default targets

What the Pixagram web app sends without an observer (`src/js/pages/Feed.js`,
`FeedPersonal.js`, `src/js/utils/api/pixaproxyapi.js`, `service-worker.js`):

| Request | Page |
|---|---|
| `bridge.get_ranked_posts {"sort":"trending","tag":"","limit":20}` | Feed: Trending; the logged-out home feed |
| `bridge.get_ranked_posts {"sort":"hot","tag":"","limit":20}` | Feed: Hot |
| `bridge.get_ranked_posts {"sort":"promoted","tag":"","limit":20}` | Feed: Promoted |
| `bridge.get_ranked_posts {"sort":"created","tag":"","limit":20}` | Feed: New (fallback) |
| `condenser_api.get_discussions_by_created [{"tag":"","limit":20}]` | Feed: New |
| `bridge.get_ranked_posts {"sort":"trending","tag":"","observer":"","limit":6}` | service worker preview |

Only first pages are worth it: later pages carry `start_author`/`start_permlink` and
spread over many keys.

## Operating it

```sql
-- what is cached, at which block, and since when it is unchanged
SELECT api, method, params, block_num, changed_block, result IS NOT NULL AS ok, error,
       pg_column_size(result) AS bytes
  FROM pixamind.feed_cache ORDER BY api, method, params;

-- keep a busy community's front page warm too (effective from the next block)
INSERT INTO pixamind.feed_cache_targets (api, method, params, note)
VALUES ('bridge', 'get_ranked_posts', '{"sort": "trending", "tag": "portal-100001", "limit": 20}', 'busy portal');

-- stop caching one
UPDATE pixamind.feed_cache_targets SET enabled = FALSE WHERE id = 3;

-- after editing indexed data by hand
SELECT pixamind.clear_feed_cache();
```

Every target is computed after every live block, inside the indexer: keep the list to
requests that are both popular and cheap enough (first pages). The refresh time is in
Prometheus.

A target's answer must depend on indexed data and the head block only, which holds for
every JSON-RPC method except those reporting ages: the `hive` and `pixamind_api`
namespaces, refused by the table. A method that sets HTTP headers or a status (no
JSON-RPC method does today; the REST endpoints, which cannot be targets, do) is stored
as failing, `not cacheable`, and always computed.

| Where | What |
|---|---|
| `pixamind_api.get_info` | `features.feed_cache`, and `feed_cache`: `targets`, `fresh` (served now), `failing`, `block`, `size_bytes` |
| `pixamind info` | `Feed cache   on, 6/6 targets fresh at block …, 0 failing` |
| Prometheus | `pixamind_feed_cache_refresh_seconds` (histogram), `pixamind_feed_cache_rewritten_total`, `pixamind_feed_cache_failures_total` |
| Logs | a warning when a refresh fails (with the traceback the first time); the per-block summary at debug level |

Useful alert: `histogram_quantile(0.95, rate(pixamind_feed_cache_refresh_seconds_bucket[10m])) > 1`
(a target became expensive).

**Standby servers.** `pixamind.feed_cache` is `UNLOGGED`: rewritten every block, it is
kept out of the WAL, so it is not replicated and a crash restart empties it (the next
block fills it again). An API served from a streaming replica computes every request,
as before; `get_info` reports `"standby": true` there.

**`hivemind_app.max_posts_per_call`.** If you set it, set it for the whole database
(`ALTER DATABASE … SET`), so the indexer and the API sessions agree; a session with
another value always computes.

## Without the Pixamind schema

`home.sql` defines a default `cached_result()` that caches nothing; the Pixamind runtime
replaces it. If the Pixamind schema is missing or its installation failed, the API
works exactly as upstream hivemind.
