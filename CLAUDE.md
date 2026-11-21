# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project

**Pixamind** is the social indexer and JSON-RPC API of the Pixa blockchain, a fork of
Hivemind 1.28.8 (HAF-based). Two parts:

1. **Indexer** (`pixamind sync`, same as `hive sync`): Python orchestration, set-based
   PL/pgSQL processing of HAF block ranges into `hivemind_app.*` tables; Pixamind's own
   phase writes `pixamind.*` (promotions, event outbox, telemetry).
2. **Server**: PostgREST + nginx rewriter in front of `hivemind_endpoints.home(json)`;
   all API logic is PL/pgSQL.

Read `docs/pixamind/ARCHITECTURE.md` before larger changes.

## Layout and the one rule that matters

```
hive/        inherited Hivemind engine — keep it close to upstream (Hivemind releases get merged)
pixamind/    Pixamind additions: conf.py, db/install.py, indexer/hooks.py, observability/, tools/, cli.py
pixamind/sql/migrations/NNNN_*.sql   tables; applied once; NEVER edit an applied migration, add a new one
pixamind/sql/runtime/*.sql           functions and API methods; reinstalled on every start (file order)
crates/pixamind-api/                 Rust API server in place of PostgREST + the nginx rewriter (calls the SQL API)
tests/pixamind/                      unit tests, SQL harness, end-to-end sync tests, API server tests
docs/pixamind/                       Pixamind docs;  docs/hivemind/README.md is the upstream README
```

New features go in `pixamind/`. Touch `hive/` only for a hook or a Pixa fix, keep the
edit small and mark it with a `Pixamind` comment. Upstream files already touched are
listed in ARCHITECTURE.md §4.

Names that stay hivemind on purpose (compatibility): DB schemas `hivemind_app`,
`hivemind_endpoints`, `hivemind_postgrest_utilities`; roles `hivemind`, `hivemind_user`;
HAF context `hivemind_app`; unix user and paths `/home/hivemind`; connection
application names `hivemind_*` (the sync loop checks them).

## Commands

```bash
python3 -m venv venv && . venv/bin/activate && pip install -e '.[dev]'

# tests: unit + SQL harness + end-to-end (needs any PostgreSQL 14+, superuser DSN)
export PIXAMIND_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
pytest tests/utils tests/pixamind

# lint/format Pixamind code (ruff targets py312: python_requires >= 3.12)
ruff check pixamind tests/pixamind tests/utils && ruff format pixamind tests/pixamind

# Rust (toolchain pinned in rust-toolchain.toml); tests/pixamind/test_api_gateway.py runs
# target/debug/pixamind-api (skipped if not built) and, with PIXAMIND_TEST_POSTGREST set
# to a PostgREST 12.0.2 binary, compares every case with PostgREST
cargo build -p pixamind-api && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test

# indexer against a HAF database
pixamind sync --database-url=postgresql://hivemind@localhost:5432/haf_block_log \
  --reptracker-schema-name=reptracker_app [--test-max-block=N] [--pixamind-events true]

# operator tools
pixamind info | integrity | events | corpus | apidiff | bench | sync-report   (--help each)
```

Docker: `docker build -t pixadock/pixamind:local .`; entrypoint commands `setup`,
`install_app`, `uninstall_app`, `sync`, `server` (see `docker/docker_entrypoint.sh`).

## Writing SQL

* SQL scripts run through `hive.db.adapter`, which turns every `:word` into a bind
  parameter — avoid `:word` outside `::casts` (e.g. no `[[:space:]]`, use `\s`).
  `tests/pixamind/test_install.py` checks the Pixamind files.
* Templates: `{{HIVE_SYMBOL}}`, `{{HBD_SYMBOL}}`, `{{VESTS_SYMBOL}}`,
  `{{LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK}}`, `{{CURATION_REWARDS_PERCENT}}` (values from
  `hive/utils/chain.py`, selected by `PIXAMIND_CHAIN`).
* Pixamind projections must be idempotent per block range (delete the range, rewrite it)
  and fail-safe (each step in a subtransaction, failures to
  `pixamind.projection_failures`). Never let them raise into the block processor.
* Never put surrogate ids (post_id, account id) in event payloads: events must be
  identical on every node.
* The shared feeds cache (`docs/pixamind/FEED-CACHE.md`) answers the requests in
  `pixamind.feed_cache_targets` from what the indexer computed for the current block.
  API methods must therefore depend only on indexed data and the head block
  (`hivemind_app.head_block_time()`), never on `now()`, `random()` or session settings
  other than `hivemind_app.max_posts_per_call`. `tests/pixamind/test_feed_cache.py`
  compares cached and computed answers byte for byte.
* The HAF context is **non-forking** (irreversible blocks only), as upstream. Don't
  register tables with HAF's fork manager: that is for forking contexts and turns off
  the massive-sync speedups. Databases from the earlier forking build keep registered
  tables until resynced (`db_state.py` handles them).

## Testing approach

`tests/pixamind/harness.py` installs the real `create_tables.sql` + runtime SQL +
Pixamind schema on plain PostgreSQL with `tests/pixamind/sql/haf_stub.sql` standing in
for HAF, logged in as the `hivemind` role. `tests/pixamind/livesync.py` runs the real
`Blocks.process_live_block_sql` / `process_multi_sql` over operations written with
`tests/pixamind/chain.py`; `scenario.py` is a small Pixa history. Prefer adding to the
scenario and asserting through the JSON-RPC API (`sql.result('method', params)`).

The inherited tavern API suite runs on Hive mainnet data (5M blocks + mocks) with
`PIXAMIND_CHAIN=hive`; see `docs/hivemind/README.md` and `docker/docker-compose-*.yml`.

## Gotchas (inherited)

* Submodules: `reputation_tracker`, `hafah`, `tests/tests_api` — clone with
  `--recurse-submodules` to build images or run tavern tests.
* Install order: HAF → reputation_tracker → Pixamind (`setup` needs the HAF admin role;
  it also creates the `pixamind` schema).
* Sync stages: MASSIVE_WITHOUT_INDEXES (> 2M blocks behind), MASSIVE_WITH_INDEXES,
  LIVE. FKs are dropped during massive sync, indexes in MASSIVE_WITHOUT_INDEXES (plus
  UNLOGGED tables on an initial sync); none of it on databases whose tables are still
  registered with the fork manager.
* PostgREST version is pinned in the Dockerfile (`pixadock/postgrest` mirror).
