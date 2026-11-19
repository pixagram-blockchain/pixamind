# Testing, determinism and benchmarks

## Test layers

| Layer | Where | Needs | Runs in GitHub CI |
|---|---|---|---|
| Unit tests | `tests/utils`, `tests/pixamind/test_tools.py`, `test_config.py` | Python | yes |
| SQL harness | `tests/pixamind/test_content_metadata.py`, `test_install.py` | any PostgreSQL 14+ | yes |
| End-to-end sync | `tests/pixamind/test_live_sync.py`, `test_massive_sync.py`, `test_integrity_tool.py` | PostgreSQL | yes |
| Inherited API suite (tavern) | `tests/api_tests/hivemind/tavern` | HAF with Hive's first 5M blocks + mocks | no (GitLab CI, `docker/docker-compose-*.yml`) |
| Load tests (k6) | `tests/performance` | a synced node | manual |

```bash
pip install -e '.[dev]'
export PIXAMIND_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
pytest tests/utils tests/pixamind
```

### The SQL harness

`tests/pixamind/harness.py` builds a database on a plain PostgreSQL server with the
**production** SQL: hivemind's tables (`create_tables.sql`), the whole runtime code that
`build_schema` installs, and the Pixamind schema, installed through the same Python
entry points, logged in as the `hivemind` role. Only HAF and reputation_tracker are
replaced, by `tests/pixamind/sql/haf_stub.sql` (context state, blocks, operations,
the handful of `hive.*` functions the SQL calls).

`tests/pixamind/livesync.py` then drives the real block processor —
`Blocks.process_live_block_sql` and `Blocks.process_multi_sql` — over synthetic blocks
(`tests/pixamind/chain.py` writes operations in HAF's JSON shape;
`tests/pixamind/scenario.py` is a small Pixa history: accounts, an artwork post,
votes, follows, a mute, a reblog, promotions, an edit, a delete, a payout). The tests
read the result back through `hivemind_endpoints.home`, i.e. the JSON-RPC API.

That makes Pixamind changes testable without a Pixa node, in seconds. It does not replace
a sync against real HAF data before a release.

## Determinism

Indexing the same blocks must give the same data, whatever the batch sizes, crashes and
replays, node or version (proposal §7). `pixamind.projection_fingerprints()` summarises
each projection — hivemind's tables and Pixamind's — as a row count and an
order-independent hash of rows rendered with natural keys, so surrogate ids do not
matter. Covered by tests:

* massive (one batch) and live (block by block) indexing of the scenario give identical
  Pixamind fingerprints;
* replaying every block's Pixamind step changes nothing;
* a micro-fork that replaces a block converges to the new fork.

On real nodes:

```bash
# two nodes, both stopped at the same block (--test-max-block=N)
pixamind integrity --database-url postgresql://…/a --compare-url postgresql://…/b
```

Differences are narrowed down by hash bucket and printed as sample rows (`only left …`).
To compare an original Hivemind database, add `--install-functions` (installs just the
fingerprint functions there) — the hivemind projections are then compared. This is the
"compare projections" half of the proposal's differential-testing diagram.

## Differential API testing

The "compare API results" half:

```bash
# requests from the inherited suite (Hive data) or sampled from a live Pixa node
pixamind corpus from-tavern --out hive.jsonl --no-negative
pixamind corpus sample --url https://pixa-node --out pixa.jsonl

pixamind apidiff --left https://hivemind-node --right https://pixamind-node \
  --corpus pixa.jsonl --report diff.json
```

Both nodes must be at the same block (stop them with `--test-max-block`, or diff right
after a block boundary and re-run differing requests). Ignore volatile fields with
`--ignore result.*.some.path`. Expected differences are listed in
[API.md](API.md#deliberate-changes).

## Benchmarks

Proposal §12 methodology, which these tools follow: identical hardware, PostgreSQL
configuration, snapshot, block range, request mix and cache state; warm and cold runs;
correctness checked after every run (`pixamind integrity`, `pixamind apidiff`).

**Indexing.** Run a sync with `--test-max-block=N` from an empty `hivemind_app`, then:

```bash
pixamind sync-report sync.log            # blocks/s and the share of each phase
```

Record blocks/s, per-phase seconds, peak RSS of the process and WAL volume
(`pg_stat_wal`). For live sync use `pixamind_api.get_metrics` (`live_latency_seconds`,
`capacity_blocks_per_second`) over a day.

**API.**

```bash
pixamind bench --url https://node --corpus pixa.jsonl --concurrency 8 --rounds 5 \
  --json bench.json --markdown bench.md
```

Per method: requests, errors, p50/p95/p99/max in ms; overall requests per second. For
ramp-up and stress profiles use the k6 scripts in `tests/performance`.

**Baseline first.** The proposal's targets (3–5× bulk indexing, −60% feed p95, …) are
relative to a baseline that does not exist yet. Phase 1 of [ROADMAP.md](ROADMAP.md) is
to record it with these tools on the current Pixa node.
