# pixamind-api: the API server in Rust

`pixamind-api` serves Pixamind's JSON-RPC and REST API in place of PostgREST and the
nginx rewriter in front of it. It calls the same SQL functions, so the SQL stays the one
implementation of the API: no method was rewritten, and every method answers byte for
byte as before. It is the first step of moving Pixamind to Rust
([ROADMAP.md](ROADMAP.md)): two runtime components fewer, with no business logic touched.

Source: [`crates/pixamind-api`](../../crates/pixamind-api). Image: [`Dockerfile.api`](../../Dockerfile.api).

## What it serves

One API port (8080 by default):

| Request | Calls |
|---|---|
| `POST` to any path no REST rule claims (`/`, `/api`, …) | `hivemind_endpoints.home(json)` with the body: JSON-RPC |
| the REST paths of [`rewrite_rules.conf`](../../rewrite_rules.conf), e.g. `GET /accounts/{name}/operations` | the function the rule names, with the path's and the query string's arguments |
| `/rpc/<function>`, PostgREST's own paths (the tavern suite and internal callers use them) | that function: query-string arguments on `GET`, a JSON object on `POST` |
| `GET /` | `hivemind_endpoints.home()`: the OpenAPI document |

`rewrite_rules.conf` is the file the nginx rewriter included; it is generated from the
SQL endpoints' OpenAPI comments (`scripts/openapi_rewrite.sh`). The server reads the same
file (bundled at build time, or `--rewrite-rules`), so a new REST endpoint needs no
change here.

An admin port (3001 by default):

| Path | |
|---|---|
| `/health` | 204, as the rewriter's port 81 answered |
| `/live` | 200 while the process runs |
| `/ready` | 200 when the database answers and the function list is loaded, else 503 |
| `/metrics` | Prometheus: requests by function, JSON-RPC method and status; latency histogram; requests in flight; pool connections; functions loaded |

## How it calls a function

As PostgREST 12 does, with the same SQL, so the bytes match:

* arguments are named and go through `json_to_record`, which converts them to the
  parameter types (a bad value is PostgreSQL's error, e.g. `22P02`, status 400);
  parameters with defaults can be left out; the overload is chosen by argument names;
* the result keeps its shape: `json` as it is, one row as an object, sets as arrays;
* a function can set `response.headers` and `response.status` (the REST endpoints set
  `Cache-Control` this way); `Content-Range` is set like PostgREST's;
* errors keep PostgREST's body, `{"code","details","hint","message"}`, and its status
  per SQLSTATE: `P0001` 400, `42883` 404, `42501` 401, `PTxyz` the status `xyz`, and so
  on; `PGRST102` (invalid JSON), `PGRST202` (no such function), `PGRST003` (pool timeout);
* CORS for any origin, header for header as PostgREST sends it.

The function list (PostgREST's schema cache) is read at start, every 60 seconds, on
`SIGUSR1`, and when a request names a function it doesn't know (at most every 2
seconds). The indexer reinstalls the SQL on upgrades; nothing needs restarting.

Each connection is set up once: the role (`SET ROLE hivemind`), `search_path`
(`hivemind_endpoints, public`), read-only transactions, and an optional
`statement_timeout`. Its `application_name` is `pixamind-api`, never `hivemind_*`: the
indexer terminates `hivemind_*` backends when it restarts.

## Checked against PostgREST

[`tests/pixamind/test_api_gateway.py`](../../tests/pixamind/test_api_gateway.py) runs the
binary on a synced test database:

* every JSON-RPC case is compared byte for byte with what `home()` returns in SQL, through
  `/`, `/api` and `/rpc/home`; REST paths, argument conversion, defaults, errors per
  SQLSTATE, `response.headers`/`status`, sets, CORS, limits, timeouts, health and metrics
  have their own tests;
* with `PIXAMIND_TEST_POSTGREST` set (CI downloads PostgREST 12.0.2, the version in
  Pixamind's image), 43 cases go to both servers, which must return the same status, the
  same bytes and the same `Content-Type`, `Content-Range`, `Cache-Control`,
  `WWW-Authenticate` and CORS headers.

What differs, on purpose:

1. **Read-only.** Queries run in read-only transactions, so a function that writes fails
   with 405 (`25006`). PostgREST runs a `VOLATILE` function read-write on `POST`.
   Pixamind's API functions only read; this guarantees it. `--read-only false` turns it off.
2. **Timeouts.** A request still running after `--request-timeout` (60 s, nginx's proxy
   timeout) is cancelled in the database and answered with 504 and a JSON body. nginx
   answered 504 with an HTML page and left the query running.
3. **Body size.** Bodies over `--max-body-bytes` (1 MiB, nginx's default) get 413 with a
   JSON body instead of nginx's HTML page.
4. **HEAD** returns the headers the function sets, as `GET` does; PostgREST leaves them out.
5. **Content-Type** of a request isn't checked: the body is read as JSON.
6. The `Server` header says `pixamind-api/<version>`.

Not supported, because Pixamind's API doesn't use them: PostgREST's operations on
function results (`select=`, `order=`, filters such as `col=eq.x`), JWT logins and
per-request roles, and HTTPS (keep Caddy or another proxy in front for TLS). JSON-RPC
batches (arrays) get `home()`'s own answer, as before.

## Speed

Measured with `oha` on a 2-CPU machine (load generator, server and PostgreSQL sharing
it), the small test database, 8 concurrent clients, and PostgREST 12.0.2 without nginx in
front, so the current setup is slower still:

| Request | PostgREST | pixamind-api |
|---|---|---|
| JSON-RPC `bridge.get_post` | 742 req/s, p99 43 ms | 1,061 req/s, p99 24 ms |
| JSON-RPC `condenser_api.get_discussions_by_promoted` | 393 req/s, p99 64 ms | 491 req/s, p99 37 ms |
| REST `/sync-status` | 1,727 req/s, p99 12 ms | 5,127 req/s, p99 4.8 ms |

The heavier the SQL, the smaller the gain: what changed is the cost around the query, not
the query. On the node, `pixamind bench` gives the real numbers.

## Running it

```bash
cargo build --release -p pixamind-api        # target/release/pixamind-api
pixamind-api --database-url postgresql://hivemind@haf:5432/haf_block_log
```

| Option | Environment | Default |
|---|---|---|
| `--database-url` | `PIXAMIND_API_DATABASE_URL`, then `POSTGRES_URL` | `postgresql://hivemind@localhost:5432/haf_block_log` |
| `--listen` | `PIXAMIND_API_LISTEN` | `0.0.0.0:8080` |
| `--admin-listen` (`off` disables) | `PIXAMIND_API_ADMIN_LISTEN` | `0.0.0.0:3001` |
| `--schema` | `PIXAMIND_API_SCHEMA` | `hivemind_endpoints` |
| `--role` (empty: the login role) | `PIXAMIND_API_ROLE` | `hivemind` |
| `--root-function` | `PIXAMIND_API_ROOT_FUNCTION` | `home` |
| `--rewrite-rules` | `PIXAMIND_API_REWRITE_RULES` | the bundled `rewrite_rules.conf` |
| `--pool-size` | `PIXAMIND_API_POOL_SIZE` | 20 (PostgREST's setting in the image) |
| `--pool-timeout` | `PIXAMIND_API_POOL_TIMEOUT` | `10s` |
| `--request-timeout` | `PIXAMIND_API_REQUEST_TIMEOUT` | `60s` |
| `--statement-timeout` | `PIXAMIND_API_STATEMENT_TIMEOUT` | the role's setting |
| `--read-only` | `PIXAMIND_API_READ_ONLY` | `true` |
| `--max-body-bytes` | `PIXAMIND_API_MAX_BODY` | 1 MiB |
| `--cors` | `PIXAMIND_API_CORS` | `true` |
| `--compression` (gzip) | `PIXAMIND_API_COMPRESSION` | `false` |
| `--schema-refresh` | `PIXAMIND_API_SCHEMA_REFRESH` | `60s` |
| `--log`, `--log-format text\|json` | `PIXAMIND_API_LOG`, `PIXAMIND_API_LOG_FORMAT` | `info`, `text` |

`pixamind-api --healthcheck` asks a running server's `/ready` and exits 0 or 1; the image's
`HEALTHCHECK` uses it.

With Docker Compose, one service replaces the PostgREST and rewriter containers:

```yaml
  pixamind-api:
    image: pixadock/pixamind-api:latest        # docker build -f Dockerfile.api .
    environment:
      PIXAMIND_API_DATABASE_URL: postgresql://hivemind@haf:5432/haf_block_log
    ports:
      - "8080:8080"                            # where clients reached the rewriter
```

## Switching over

1. Run `pixamind-api` next to the current PostgREST and rewriter, on another port,
   against the same database. It only reads.
2. Compare the two on real requests:
   `pixamind corpus sample --url http://current:80 --out corpus.jsonl`, then
   `pixamind apidiff --left http://current:80 --right http://new:8080 --corpus corpus.jsonl`.
   `pixamind bench --url … --corpus corpus.jsonl` measures each.
3. Point Caddy (or the proxy in front) at the new port. Keep the old containers stopped
   but not removed until you're confident; switching back is the reverse.
4. Remove the PostgREST and rewriter containers.

HAfAH and the reputation tracker have their own PostgREST and rewriter. The server can
serve them too (`--schema`, `--role`, `--rewrite-rules`), one process per API, once their
rules are checked: HAfAH's use an nginx variable (`$path_filters`) that the parser
rejects, so it needs a small addition first.
