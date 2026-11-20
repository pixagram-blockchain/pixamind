# Operating Pixamind

## Running

Pixamind runs like the Hivemind it replaces (see the README for Docker commands):
HAF → reputation_tracker → `setup` → `sync`, then `server` (PostgREST) behind the nginx
rewriter, or `pixamind-api` in place of both. Everything Pixamind adds runs inside `sync`
and the API server; there is no extra service.

## Configuration

| Option | Environment | Default | Effect |
|---|---|---|---|
| `--pixamind-promotions` | `PIXAMIND_PROMOTIONS` | true | promoted feed |
| `--pixamind-events` | `PIXAMIND_EVENTS` | false | event outbox |
| `--pixamind-events-retention-days` | `PIXAMIND_EVENTS_RETENTION_DAYS` | 30 | 0 = keep all |
| `--pixamind-telemetry` | `PIXAMIND_TELEMETRY` | true | per-batch telemetry |
| `--prometheus-port` | `PROMETHEUS_PORT` | — | expose Prometheus metrics (inherited option) |
| `--community-start-block` | `COMMUNITY_START_BLOCK` | 1 | |
| — | `PIXAMIND_CHAIN` | pixa | `hive` for Hive data (API test suite) |

Chain symbol and correction overrides are listed in [MIGRATION.md](MIGRATION.md).

## Watching it

### `pixamind_api.get_metrics` (JSON-RPC)

For dashboards that cannot scrape Prometheus, such as the Systemic Control Tower on
Cloudflare. It covers the proposal's "engine health" panel: indexer lag
(`lag.*`), throughput (`throughput.*`), block-to-API latency (`live_latency_seconds`),
per-phase timings (`phase_seconds_avg`) and projection health (`failures`). API latency
as clients see it is measured from the outside (`pixamind bench`, k6).

```bash
pixamind info --url https://<node>          # the same, human-readable
```

Telemetry is kept 7 days in `pixamind.sync_batches` (one row per live block).

### Prometheus (`--prometheus-port 11011`)

| Metric | Type | |
|---|---|---|
| `pixamind_build_info{version,hivemind_base,chain}` | info | |
| `pixamind_indexer_last_block` | gauge | last committed block |
| `pixamind_indexer_blocks_total{mode}` | counter | `mode` = massive / live |
| `pixamind_indexer_batches_total{mode}` | counter | |
| `pixamind_indexer_batch_seconds{mode}` | histogram | time per batch / live block |
| `pixamind_indexer_phase_seconds_total{mode,phase}` | counter | where the time goes |
| `pixamind_block_to_commit_seconds` | histogram | live: block timestamp → indexed |
| `pixamind_events_written_total` | counter | |
| `pixamind_promotions_indexed_total` | counter | |
| `pixamind_projection_failures_total` | counter | alert on increase |

Plus the inherited `hivemind_sync_current_block_blocks` gauge. Useful alerts:

* a Pixamind step failed: `increase(pixamind_projection_failures_total[15m]) > 0`
* the API falls behind the chain: `histogram_quantile(0.95, rate(pixamind_block_to_commit_seconds_bucket[10m])) > 10`.
  The context is non-forking, so this includes the wait for the block to become
  irreversible (normally under a block). A rise while the indexer's own batch times stay
  flat points at the witnesses (missed blocks or confirmations), not at Pixamind.
* the indexer stalled: `changes(pixamind_indexer_last_block[2m]) == 0`

With `pixamind-api` in place of PostgREST ([API-SERVER.md](API-SERVER.md)), its admin port
(3001) serves `/metrics` too: `pixamind_api_requests_total{function,method,status}`,
`pixamind_api_request_duration_seconds`, `pixamind_api_requests_in_flight`,
`pixamind_api_db_connections{state}`. Alerts worth having:

* errors: `sum(rate(pixamind_api_requests_total{status=~"5.."}[5m])) > 0`
* slow methods: `histogram_quantile(0.95, sum by (le, method) (rate(pixamind_api_request_duration_seconds_bucket[10m]))) > 1`
* pool exhausted: `pixamind_api_db_connections{state="waiting"} > 0` for several minutes

### Logs

Massive batches log `[PHASE-SUMMARY] blocks=a-b total=…s load=… posts=… pixamind=…`;
live blocks log `[PROCESS LIVE SQL] blocks n-n in …s`. Summarise a log with:

```bash
pixamind sync-report hivemind_sync.log
```

## When a Pixamind step fails

The indexer does not stop. The range goes to `pixamind.projection_failures` and
`get_metrics.failures`; a warning is logged. Fix the cause, then rebuild the range:

```bash
pixamind events backfill --database-url postgresql://hivemind@haf/haf_block_log --from-block A --to-block B
```

(it rebuilds events and promotions for A..B from HAF and marks the failures resolved).
If the Pixamind schema itself could not be installed (e.g. no `setup` run after
upgrading), all Pixamind features are off for that run and the log says so at startup.

## Checking data

```bash
pixamind integrity --database-url postgresql://…/haf_block_log                 # fingerprints
pixamind integrity --database-url A --compare-url B                            # two nodes
```

Two nodes stopped at the same block must agree on every projection; see
[TESTING.md](TESTING.md).

## Uninstalling

`uninstall_app` (Docker entrypoint) removes the hivemind schemas and the `pixamind`
schema.
