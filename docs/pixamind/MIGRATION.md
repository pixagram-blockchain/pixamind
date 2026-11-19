# From pixagram-blockchain/hivemind to Pixamind

Pixamind 0.1.0 is a drop-in replacement for the Pixa Hivemind deployment: same database
schemas, roles, Docker entrypoint commands and JSON-RPC API. What changes is the name on
the outside, a few defaults, and the additions.

## What is renamed, what is not

| | Hivemind | Pixamind 0.1.0 |
|---|---|---|
| Repository | `pixagram-blockchain/hivemind` | `pixagram-blockchain/pixamind` |
| Python distribution | `hivemind` | `pixamind` (packages `hive` + `pixamind`) |
| Command | `hive sync` | `pixamind sync` (`hive` still installed) |
| Docker image | `pixadock/hivemind` | `pixadock/pixamind` |
| Entrypoint commands | `setup`, `install_app`, `uninstall_app`, `sync`, `server` | unchanged |
| Chain symbols | `HIVEMIND_HIVE_SYMBOL`, `HIVEMIND_HBD_SYMBOL` | `PIXAMIND_HIVE_SYMBOL`, `PIXAMIND_HBD_SYMBOL` (old names still read) |
| Default symbols | HIVE / HBD | PIXA / PXS |
| Default community start block | 37,500,000 | 1 |
| Compose image variable | `HIVEMIND_IMAGE` | `PIXAMIND_IMAGE` |
| Database schemas | `hivemind_app`, `hivemind_endpoints`, `hivemind_postgrest_utilities` | unchanged, plus `pixamind` |
| Database roles, unix user, container paths | `hivemind`, `hivemind_user`, `/home/hivemind` | unchanged |
| HAF context / registry name | `hivemind_app` | unchanged |

Keeping the database names is deliberate (the proposal's §15: no destructive migration
in the first release). Other HAF applications and operator scripts that know
`hivemind_app` keep working, and upstream Hivemind releases still merge cleanly.

## New settings

| Option | Environment | Default | |
|---|---|---|---|
| — | `PIXAMIND_CHAIN` | `pixa` | `hive` restores Hivemind's Hive-specific behaviour (used by the API test suite) |
| `--pixamind-promotions` | `PIXAMIND_PROMOTIONS` | `true` | index promotions, serve the promoted feed |
| `--pixamind-events` | `PIXAMIND_EVENTS` | `false` | write the event outbox |
| `--pixamind-events-retention-days` | `PIXAMIND_EVENTS_RETENTION_DAYS` | `30` | `0` keeps everything |
| `--pixamind-telemetry` | `PIXAMIND_TELEMETRY` | `true` | per-batch timings for `get_metrics` |
| — | `PIXAMIND_COMMUNITY_START_BLOCK` | `1` | default of `--community-start-block` |
| — | `PIXAMIND_CURATION_REWARDS_PERCENT` | `4000` | curators' share in basis points (pending-reward endpoints) |
| — | `PIXAMIND_LEGACY_RSHARES_UPSCALE_BEFORE_BLOCK` | `0` | Steem HF1 correction; `905693` on Hive |

## Upgrading a node

1. Build or pull `pixadock/pixamind` (see the README) and point the compose file at it.
2. Run the `setup` command once with the HAF admin URL, as for any Hivemind upgrade. It
   creates the `pixamind` schema (creating a schema needs the admin role; the sync role
   usually cannot). Without this step the indexer still runs, with Pixamind features off
   and a warning in the log.
3. Start `sync` and `server` as before. Optionally set `PIXAMIND_EVENTS=true`.
4. Check: `pixamind info --url <api>` or the `pixamind_api.get_info` method.

### Resync recommended

Two corrections apply to data written from now on, not to rows already stored:

* vote rshares of blocks below 905,693 (×10⁶ too large in the current database);
* `max_accepted_payout` (and other amounts formatted by `legacy_amount`) stored as `HBD`.

Hivemind's tables are registered with HAF's fork manager on Pixa, which rejects edits
outside block processing, so the clean fix is a resync — about 1.2M blocks in October
2026, a short job:

```bash
docker run --rm ... pixadock/pixamind:<tag> uninstall_app --database-admin-url=postgresql://haf_admin@haf/haf_block_log
docker run --rm ... pixadock/pixamind:<tag> setup --database-admin-url=postgresql://haf_admin@haf/haf_block_log
docker run -d   ... pixadock/pixamind:<tag> sync --database-url=postgresql://hivemind@haf/haf_block_log
```

Before switching API traffic, compare the new node with the old one (see
[TESTING.md](TESTING.md)):

```bash
pixamind corpus sample --url https://old-node --out pixa.jsonl
pixamind apidiff --left https://old-node --right https://new-node --corpus pixa.jsonl
```

Expected differences: amounts in `PXS` instead of `HBD`, rshares of first-month votes,
and anything about promoted posts.

## Later: renaming the database objects

A future release can move `hivemind_app` to `pixamind_app` (and the role names). HAF has
no in-place context rename, so it will be a fresh sync into the new context name,
followed by switching PostgREST (`PGRST_DB_SCHEMAS`) and retiring the old schema — cheap
on a chain this size. It only makes sense once no other HAF application depends on the
`hivemind_app` name.
