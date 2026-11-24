# Pixamind architecture

Pixamind is the social indexer and query API of the Pixa blockchain. It is a fork of
Hivemind 1.28.8 (by way of `pixagram-blockchain/hivemind`). This document describes
the system as it is in this repository, how it differs from what the November 2026
modernization proposal assumed, and where Pixamind's additions sit.

## 1. The system as built

```
 hived (Pixa) ──► HAF (PostgreSQL: hafd.* tables, per-app views)
                     │
                     │  hive.app_next_iteration()  ── block ranges for the hivemind_app context
                     ▼
 ┌──────────────── indexer (Python orchestration, SQL work) ───────────────────────┐
 │ massive sync: batches of up to 1000 blocks         live sync: one block at a time │
 │  1 load_ops_staging      one scan of operations_view into hivemind_app._ops_staging │
 │  2 accounts + community state ops                                                  │
 │  3 posts (set-based waves), 3a community post ops                                  │
 │  3.5 votes (rshares/sc_trend/sc_hot of touched posts)                              │
 │  4 reblogs ∥ follows ∥ payouts  (parallel connections)  + post bodies (Python)     │
 │  5 post data flush, mentions   6 notification cache   7 boundary vacuum            │
 │  8 Pixamind: promotions, event outbox, telemetry   ◄── added by Pixamind           │
 └──────────────────────────────────────────────────────────────────────────────────┘
                     │ writes
                     ▼
 hivemind_app.*  (posts, votes, follows, communities, feed cache, notification cache, ...)
 pixamind.*      (events, post_promotions, promoted_posts, sync_batches, ...)
                     │ reads
                     ▼
 nginx (JSON-RPC rewrite) ─► PostgREST ─► hivemind_endpoints.home(json)
   or pixamind-api (Rust, both in one) ─┘
                                            └► hivemind_postgrest_utilities.dispatch
                                                 ├ bridge, condenser_api, follow_api, tags_api,
                                                 │ database_api, hive, search-api  (inherited)
                                                 └ pixamind_api, pixamind_content,
                                                   pixamind_graph, pixamind_feed   (Pixamind)
```

* **Indexing is SQL.** Upstream moved block processing into set-based PL/pgSQL over a
  staging table during 2025–2026 (`hive/db/sql_scripts/massive_sync.sql`). Python
  drives the loop, runs phases in parallel on separate connections and still merges
  post-body diffs (`diff_match_patch`); everything else is SQL.
* **Crash safety.** A massive batch is recorded in `hivemind_app._batch_queue` before it
  runs and replayed after a crash; replays are idempotent. A live block commits its work,
  `last_completed_block` and the HAF context advance in one transaction (upstream #336).
* **Projections are already incremental.** Votes recompute the rshares and the
  `sc_trend`/`sc_hot` scores of the posts they touch; feeds (`hive_feed_cache`) and
  notifications (`hive_notification_cache`, 90-day window) are maintained per batch;
  trending/hot/payout listings read precomputed scores through partial indexes.
* **Relationships are adjacency tables**: `follows`, `muted`, `blacklisted`,
  `follow_muted`, `follow_blacklisted`, each with a `(follower, following)` primary key
  and a `following` index.
* **API is SQL too**: PostgREST exposes `hivemind_endpoints.home`, which dispatches
  JSON-RPC methods to PL/pgSQL functions; there is no Python server. `pixamind-api`, in
  Rust, can take the place of nginx and PostgREST: it calls the same functions and
  answers byte for byte as PostgREST does ([API-SERVER.md](API-SERVER.md)).

### Pixa specifics inherited from `pixagram-blockchain/hivemind`

| What | Where | Note |
|---|---|---|
| Communities are `portal-NNNNNN` accounts (Hive: `hive-NNNNNN`) | community.sql, check_community.sql, community.py | |
| Asset symbols PIXA / PXS (NAIs are Hive's) | `{{HIVE_SYMBOL}}`/`{{HBD_SYMBOL}}` SQL templates | now defaults, see `PIXAMIND_CHAIN` |
| Curators get 40% of post rewards | `hive_curation_rewards_percent()` | now `{{CURATION_REWARDS_PERCENT}}` |
| A forking HAF context, with every table registered with HAF's fork manager (HFM) | `haf_functions.py`, `schema.py` | **reverted**: non-forking again, as upstream (§3.2) |
| Massive-sync speedups (drop indexes/FKs, UNLOGGED tables) skipped for registered tables | `db_state.py` | kept for databases set up by the forking build |
| A sync from scratch always takes the full finalization path (the chain is shorter than the 2M-block threshold) | `db_state.py` | |

## 2. The proposal against the code

The proposal (November 2026) was written from the README and Hive documentation. Several
of its premises do not hold for 1.28.8; Pixamind's first release is planned around what
the code actually does.

| Proposal assumption | What 1.28.8 does | Pixamind |
|---|---|---|
| §4.1 operation-by-operation Python writes, many round trips | ~20 set-based SQL statements per batch over a staging table, phases in parallel | kept as is; **measured** first: per-phase telemetry, Prometheus, `pixamind sync-report` |
| §4.2 projections must become incremental | they are (votes → touched posts, feed and notification caches per batch, idempotent replay) | new projections follow the same per-range, idempotent pattern |
| §4.4 rankings recomputed per request | `sc_trend`/`sc_hot` stored and indexed; payout uses an expression index | promoted ranking added; ranking-algorithm versioning is on the roadmap |
| §4.5 graph tables needed | five adjacency tables with forward and reverse indexes | batch relationship lookups (`pixamind_graph`) |
| §5.3 notifications aggregated at request time | projected at sync time into `hive_notification_cache` | unchanged |
| §6 event-driven architecture with an outbox | absent | **added**: `pixamind.events` + `pixamind_api.get_events` (opt-in) |
| §7 only irreversible blocks are indexed | upstream: yes; Pixa's fork switched to **reversible** blocks (forking context) | irreversible only again (§3.2); the outbox is fork-safe either way |
| §8.4 metrics for the Control Tower | logs and one Prometheus gauge | **added**: metrics, `pixamind.sync_batches`, `pixamind_api.get_metrics` |
| §7/§13 determinism and differential tests | tavern API suite on Hive data | **added**: projection fingerprints, `pixamind integrity`, `pixamind apidiff`, SQL harness |
| §15 rename `hivemind_app` → `pixamind_app` | — | deferred (non-destructive first release, as §15 itself recommends) |

## 3. Findings specific to Pixa

### 3.1 Fixed in this release

1. **Vote rshares inflated a million times in Pixa's first month.** Hivemind multiplies
   the rshares of `effective_comment_vote` operations below block 905,693 by 10⁶: Steem's
   HF1 (April 2016) changed the unit. On Pixa, with its own genesis, those are the
   chain's first ~31 days. `PIXAMIND_CHAIN=pixa` (default) disables the correction; it
   stays on with `PIXAMIND_CHAIN=hive`. Affects `hive_votes.rshares`, post
   `vote_rshares`/`abs_rshares` and `net_rshares`/`rshares` in API answers for those posts.
2. **`HBD` in Pixa API answers.** `hivemind_app.legacy_amount()` hardcoded `HIVE`/`HBD`.
   Every post with `comment_options` — all Pixagram posts, which set a beneficiary —
   stored and returned e.g. `max_accepted_payout: "1000000.000 HBD"`. Now templated
   (`PXS`). Reproduced and covered by `tests/pixamind/test_live_sync.py`.
3. **Community start block** defaulted to Hive's 37,500,000; the Pixa default is now 1
   (`--community-start-block` / `COMMUNITY_START_BLOCK` still override it).
4. **Promoted posts**, removed upstream in January 2025, are back
   ([PROMOTED_POSTS.md](PROMOTED_POSTS.md)).

Items 1 and 2 are corrected for new data; rows already stored keep the old values until
a resync (the Pixa chain is small: about 1.2M blocks in October 2026). See
[MIGRATION.md](MIGRATION.md).

### 3.2 Forking mode: turned off

Pixa's fork switched the HAF context to *forking* (`_is_forking => TRUE`) and registered
every table with HAF's fork manager, so that the API showed a block as soon as it was
produced. Pixamind is back to upstream's non-forking context: only irreversible blocks
are indexed.

* **Latency:** Pixa runs all of its scheduled witnesses, so one-block irreversibility
  keeps the last irreversible block within about a block of the head. The API trails the
  chain by a few seconds at most, not the 45–60 seconds irreversibility takes without
  fast confirmations.
* **Gains:** no shadow-table writes in live sync, and upstream's massive-sync speedups
  are back: foreign keys are dropped during massive sync, and a sync more than 2M blocks
  behind also drops indexes and, on an initial sync, uses UNLOGGED tables (upstream
  measured ~355 vs ~137 blocks/s). The registration workarounds are gone
  (`haf_functions.py` is upstream's again, and `schema.py` no longer registers tables).
* **Existing databases:** `setup` switches the context to non-forking. HAF rewinds it to
  the last irreversible block and the indexer re-indexes from there (Pixamind's
  projections rewrite those blocks). The old registrations stay, so `db_state.py` still
  skips the index and FK juggling for such a database; a resync, recommended anyway
  (§3.1), gives a clean one.
* **Watch:** `pixamind_block_to_commit_seconds` (and `live_latency_seconds` in
  `get_metrics`) now include the wait for irreversibility. If witnesses miss blocks or
  confirmations it grows, and the alert in [OPERATIONS.md](OPERATIONS.md) fires. Going
  back to forking would mean reverting this change; Pixamind's own projections work in
  both modes.

### 3.3 Noted, not changed

* `_last_safe_cashout_block` uses Hive's HF17 block (10,629,455); on Pixa it is always 0,
  which turns off a massive-sync shortcut (performance only).
* Follow and reblog payloads in the legacy object form are accepted below block 6,000,000
  (a Steem-era allowance).
* `block_before_head`/`block_before_irreversible` assume 3-second blocks.
* The `reputation_tracker` submodule was not reviewed for similar Steem-history constants.

## 4. Code layout

```
hive/        inherited engine, kept close to upstream so Hivemind releases can be merged
pixamind/    everything Pixamind adds
  conf.py              --pixamind-* options
  db/install.py        installer for the pixamind schema (migrations + runtime SQL)
  indexer/hooks.py     phase 8: called by hive/indexer/blocks.py after each batch/block
  observability/       Prometheus metrics
  tools/               pixamind info | integrity | events | corpus | apidiff | bench | sync-report
  sql/migrations/      NNNN_*.sql: tables, applied once, recorded in pixamind.schema_migrations
  sql/runtime/         functions and API methods, reinstalled on every start
crates/pixamind-api/  the API server in Rust (in place of PostgREST and the nginx rewriter)
tests/pixamind/       unit tests, SQL harness (real SQL over a HAF stub), end-to-end sync tests,
                      the API server against the SQL and against PostgREST
docs/pixamind/        this documentation
```

Upstream files touched by Pixamind are few and marked with `Pixamind` comments:
`hive/conf.py`, `hive/db/schema.py`, `hive/db/db_state.py`, `hive/indexer/blocks.py`
(the hooks after each batch, live block and live COMMIT, and where each comment operation
is merged, for the post history), `hive/indexer/sync.py`,
`hive/indexer/community.py`, `hive/utils/chain.py`, and in SQL `massive_sync.sql`,
`assets_operations.sql`, `dispatch.sql`, `get_api_method.sql`, `home.sql` (Pixamind
methods, `SET jit = off`, the shared feeds cache lookup), `bridge_api_get_ranked_posts.sql`, and
`create_bridge_post_object.sql`, `create_condenser_post_object.sql`,
`create_database_post_object.sql` (the `nft_minted` field of every post, [NFT.md](NFT.md); the
bridge and condenser builders also add `promoted`, [PROMOTED_POSTS.md](PROMOTED_POSTS.md)).

## 5. Guarantees of the Pixamind additions

* **Never stop the social indexer.** Each Pixamind step runs in its own subtransaction;
  in live sync the whole hook runs under a savepoint inside the block's transaction. A
  failure is written to `pixamind.projection_failures`, counted in Prometheus and shown
  by `get_metrics`; the block or batch completes. The installer is fail-safe too.
* **Atomic with hivemind's data.** In live sync Pixamind's rows commit in the same
  transaction as the block; in massive sync they commit right after the batch and are
  replayed with it after a crash.
* **Deterministic.** Pixamind rows are derived from chain data only (no surrogate ids in
  event payloads); event ids are HAF operation ids. Massive and live indexing produce
  identical fingerprints (`tests/pixamind/test_massive_sync.py`).
* **No stale answers from the cache.** The shared feeds cache ([FEED-CACHE.md](FEED-CACHE.md))
  serves a row only while the context is still at the block it was computed for, with
  the bytes the API produced then (only the reputation tracker's data, synced by its own
  process, can be up to a block older); every other request is computed as upstream.
* **Fork-safe without HFM.** Each batch deletes and rewrites its own block range, so
  re-processed blocks replace abandoned ones; readers of the outbox see only blocks the
  context has processed, and by default only irreversible ones.
