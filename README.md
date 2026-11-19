# Pixamind

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The social indexing and query engine of the Pixa blockchain.**

Pixamind interprets the social operations of the Pixa chain — posts and artworks,
votes, follows, reblogs, communities (`portal-…`), payouts, promotions — and serves
them to Pixagram and other applications through a JSON-RPC API. It is a fork of
[Hivemind](https://github.com/openhive-network/hivemind) 1.28.8, built on the Hive
Application Framework (HAF), and stays compatible with Hivemind's API: existing clients
work unchanged.

The chain is the source of truth. Everything Pixamind stores is derived from blocks and
can be rebuilt from them.

## What Pixamind adds to Hivemind

| | |
|---|---|
| **Promoted posts** | Burn PXS to `@null` with memo `@author/permlink` and the post climbs the promoted feed (`bridge.get_ranked_posts` sort `promoted`). Upstream had removed it. [→](docs/pixamind/PROMOTED_POSTS.md) |
| **Event outbox** | A cursor-paged, fork-safe stream of social events (`pixamind_api.get_events`) for Pixagram Search, the Control Tower and other services. [→](docs/pixamind/EVENTS.md) |
| **Artwork metadata** | Format, pixel size, animation, licence and AI disclosure of each post, read from chain data (`pixamind_content.get_content_metadata`). [→](docs/pixamind/API.md#pixamind_content) |
| **Engine health** | Lag, throughput, block-to-API latency, per-phase timings: `pixamind_api.get_metrics` and Prometheus. [→](docs/pixamind/OPERATIONS.md) |
| **Integrity and differential testing** | Projection fingerprints to prove two databases agree; API diff and benchmark tools; a SQL test harness that runs the real indexer without a HAF node. [→](docs/pixamind/TESTING.md) |
| **Pixa fixes** | Vote rshares of the chain's first month no longer ×10⁶, `PXS` instead of `HBD` in amounts, communities from block 1. [→](docs/pixamind/ARCHITECTURE.md#3-findings-specific-to-pixa) |

## How it works

```
hived (Pixa) ─► HAF (PostgreSQL) ─► pixamind sync ─► hivemind_app.* + pixamind.*
                                                          │
                     clients ─► nginx ─► PostgREST ─► hivemind_endpoints.home (JSON-RPC)
```

* **Indexer** (`pixamind sync`): pulls block ranges from HAF and processes them with
  set-based SQL — large batches while catching up, one block at a time at the head. After
  each batch, Pixamind's own phase indexes promotions, writes the event outbox and
  records telemetry; it can never stop the core indexer.
* **API server** (`server`): PostgREST runs PL/pgSQL functions; there is no Python in the
  request path.

Details: [docs/pixamind/ARCHITECTURE.md](docs/pixamind/ARCHITECTURE.md).

## Running it

Pixamind needs a HAF database of the Pixa chain with
[reputation_tracker](https://github.com/openhive-network/reputation_tracker) installed,
like Hivemind. The order is HAF → reputation_tracker → Pixamind.

```bash
# install (once per upgrade; needs the HAF admin role)
docker run --rm --network=haf pixadock/pixamind:<tag> setup \
  --database-admin-url=postgresql://haf_admin@haf:5432/haf_block_log

# indexer
docker run -d --network=haf --name=pixamind pixadock/pixamind:<tag> sync \
  --database-url=postgresql://hivemind@haf:5432/haf_block_log --prometheus-port 11011

# API (PostgREST) and the JSON-RPC rewriter
docker run -d --network=haf --name=pixamind-server pixadock/pixamind:<tag> server \
  --database-url=postgresql://hivemind@haf:5432/haf_block_log
docker run -d --network=haf -p 8080:80 --name=pixamind-rewriter <rewriter image>

curl -s localhost:8080 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"pixamind_api.get_info"}'
```

Build the images from this repository (`git clone --recurse-submodules`):

```bash
docker build -t pixadock/pixamind:local .
docker build -t pixadock/pixamind-rewriter:local -f Dockerfile.rewriter .
```

Coming from `pixagram-blockchain/hivemind`? Read [MIGRATION.md](docs/pixamind/MIGRATION.md)
first: names, defaults, and why a resync is recommended.

### Configuration

| Option | Environment | Default | |
|---|---|---|---|
| `--database-url` | `DATABASE_URL` | | HAF database |
| `--pixamind-promotions` | `PIXAMIND_PROMOTIONS` | `true` | promoted feed |
| `--pixamind-events` | `PIXAMIND_EVENTS` | `false` | event outbox |
| `--pixamind-events-retention-days` | `PIXAMIND_EVENTS_RETENTION_DAYS` | `30` | |
| `--pixamind-telemetry` | `PIXAMIND_TELEMETRY` | `true` | |
| `--prometheus-port` | `PROMETHEUS_PORT` | | metrics endpoint |
| `--community-start-block` | `COMMUNITY_START_BLOCK` | `1` | |
| | `PIXAMIND_CHAIN` | `pixa` | `hive` reproduces Hivemind on Hive data |
| | `PIXAMIND_HIVE_SYMBOL` / `PIXAMIND_HBD_SYMBOL` | `PIXA` / `PXS` | `HIVEMIND_*` names still accepted |

`pixamind sync --help` lists every option.

## API

All of Hivemind's namespaces — `bridge`, `condenser_api`, `follow_api`, `tags_api`,
`database_api`, `hive`, `search-api` — plus `pixamind_api`, `pixamind_content`,
`pixamind_graph` and `pixamind_feed`. Reference and examples:
[docs/pixamind/API.md](docs/pixamind/API.md).

## Operator tools

```
pixamind info         node, lag, throughput and latency of a running node
pixamind integrity    projection fingerprints; compare two databases row by row
pixamind events       backfill the event outbox for a block range, or tail it
pixamind corpus       request corpus from the API test suite or sampled from a node
pixamind apidiff      replay a corpus against two nodes and report differences
pixamind bench        latency percentiles and throughput for a corpus
pixamind sync-report  where indexing time goes, from an indexer log
```

## Development

```bash
python3 -m venv venv && . venv/bin/activate
pip install -e '.[dev]'
export PIXAMIND_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
pytest tests/utils tests/pixamind        # unit, SQL harness and end-to-end sync tests
ruff check pixamind tests/pixamind && ruff format pixamind tests/pixamind
```

The SQL tests need only a PostgreSQL server: they install the production SQL over a
small HAF stand-in and drive the real block processor over synthetic Pixa blocks.

Code layout: `hive/` is the inherited engine, kept close to upstream so Hivemind
releases can still be merged; `pixamind/` holds everything Pixamind adds, with its SQL in
`pixamind/sql/`. The inherited API test suite (tavern) and its HAF-based CI are described
in the [upstream README](docs/hivemind/README.md).

## Documentation

* [Architecture](docs/pixamind/ARCHITECTURE.md) — the system as built, findings, guarantees
* [Roadmap](docs/pixamind/ROADMAP.md) — the modernization plan against the code
* [Migration](docs/pixamind/MIGRATION.md) — from `pixagram-blockchain/hivemind`
* [API](docs/pixamind/API.md) · [Events](docs/pixamind/EVENTS.md) · [Promoted posts](docs/pixamind/PROMOTED_POSTS.md)
* [Operations](docs/pixamind/OPERATIONS.md) · [Testing and benchmarks](docs/pixamind/TESTING.md)
* Upstream: [Hivemind README](docs/hivemind/README.md), [communities](docs/communities.md), [follows](docs/follows.md)

## License

MIT, like Hivemind. See [LICENSE](LICENSE).
