# Pixamind roadmap

The November 2026 proposal's phases, re-planned against the code as it is
([ARCHITECTURE.md](ARCHITECTURE.md) §2). ✅ done · ◐ partly · ○ to do.

## Phase 1 — Foundation and profiling (P0)

| | Item | |
|---|---|---|
| ✅ | Audit of the Python and SQL code | ARCHITECTURE.md §1–3 |
| ✅ | API compatibility documented | API.md |
| ✅ | Deterministic replay tests | fingerprints, massive-vs-live and replay tests, `pixamind integrity` |
| ✅ | Benchmark tooling | `pixamind bench`, `pixamind sync-report`, telemetry, Prometheus |
| ✅ | Test bed without a HAF node | SQL harness + real block processor (TESTING.md) |
| ○ | **Baselines on the Pixa node** | full resync with `--test-max-block` → `sync-report`; a week of `get_metrics`; `bench` on a sampled corpus. Every later target is relative to this. |
| ✅ | **Forking vs. irreversible decision** | irreversible only: with all witnesses running, one-block irreversibility keeps the API within a block of the head (ARCHITECTURE.md §3.2) |

Deliverable: the baseline report — the tools are ready; it needs the production node.

## Phase 2 — Indexing engine (P0)

Most of what the proposal listed (bulk SQL, batching, set-based projections, replay,
checkpoints) already exists upstream. What is left is driven by the Phase 1 numbers:

| | Item |
|---|---|
| ✅ | Non-forking context without HFM registration: the massive-sync speedups are back (on a database set up or resynced by Pixamind) |
| ○ | Profile the phases that dominate `sync-report` (posts and votes are the usual suspects) |
| ○ | Move post-body diff merging (the last Python hot path) into SQL or a native extension, if it shows up |
| ○ | Re-enable the HF17 cashout shortcut for Pixa (`_last_safe_cashout_block`) after verifying Pixa's 7-day cashout from genesis |
| ○ | Adaptive batch size (HAF stage `blocks_limit_in_group`) once batch timings are known |

## Phase 3 — Database and queries (P0)

| | Item |
|---|---|
| ○ | Capture the slowest API calls with `pixamind bench` + `pg_stat_statements`/`auto_explain` (`--log_explain_queries`) |
| ○ | Index review against real plans and write amplification (the proposal's §4.3 list, validated, not applied blindly) |
| ○ | Cursor pagination for remaining offset-style methods |
| ○ | Rename `hivemind_app` → `pixamind_app` via a fresh sync (MIGRATION.md) |

## Phase 4 — Social engine (P1)

| | Item | |
|---|---|---|
| ✅ | Event-driven projection outbox | `pixamind.events`, EVENTS.md |
| ✅ | Promoted feed | PROMOTED_POSTS.md |
| ✅ | Batch relationship lookups, mutual follows | `pixamind_graph` |
| ○ | Ranking algorithm versioning and parameters (hot/trending as configurable, versioned functions) | |
| ○ | Personal and recommended feeds from the follow graph | |
| ○ | Notification preferences and grouping | |

## Phase 5 — Pixa-native extensions (P1)

| | Item | |
|---|---|---|
| ✅ | Artwork metadata: format, size, animation, licence, AI disclosure | `pixamind_content` |
| ✅ | Search integration contract | event outbox + content metadata |
| ○ | Switch Pixagram Search from polling to `get_events` | in pixagram-search |
| ○ | Control Tower panel on `get_metrics` | in the Control Tower |
| ○ | Proof-of-Brain analytics (engagement velocity, curation quality) as projections | |
| ○ | Promotion spending and creator payout analytics | data is in `pixamind.post_promotions` and `author_reward` events |

## Phase 6 — Native performance (P2)

Only where Phase 1–3 measurements show a hot path that the whole pipeline would gain
from (proposal §8.1): candidates are operation decoding and post-body merging.

## Release practice

* Merge upstream Hivemind releases into `hive/` (the touched upstream files are listed in
  ARCHITECTURE.md §4), then run the full test suite.
* New tables: a new `pixamind/sql/migrations/NNNN_*.sql`; never edit an applied one.
* Before tagging: harness tests, a sync of the Pixa chain to a fixed block, `pixamind
  integrity` against the previous release, `pixamind apidiff` on a sampled corpus.
