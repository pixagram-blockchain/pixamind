-- Pixamind 0001: core tables of the `pixamind` schema.
--
-- Everything here is derived data that can be rebuilt from the chain. None of
-- these tables is registered with HAF's fork manager: projections that follow
-- reversible blocks converge on their own (each batch deletes and rewrites its
-- block range), and readers only see blocks the hivemind_app context has
-- processed. See docs/pixamind/EVENTS.md.

-- Key/value settings written by the indexer at startup (feature switches, retention).
CREATE TABLE IF NOT EXISTS pixamind.settings (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per processed batch (massive sync) or block (live sync).
CREATE TABLE IF NOT EXISTS pixamind.sync_batches (
    id              BIGSERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    mode            TEXT NOT NULL CHECK (mode IN ('massive', 'live')),
    first_block     INT NOT NULL,
    last_block      INT NOT NULL,
    block_time      TIMESTAMP,                -- timestamp of last_block (UTC)
    ops             INT NOT NULL DEFAULT 0,   -- operations hivemind loaded for the batch
    seconds         REAL NOT NULL,            -- indexer wall time for the batch
    latency_seconds REAL,                     -- live only: time from block production to this record
    phases          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sync_batches_recorded_at_idx ON pixamind.sync_batches (recorded_at);

-- A Pixamind projection step that failed for a block range. The indexer keeps
-- going (Pixamind extensions never stop the core social indexer); the failure
-- stays visible here and in pixamind_api.get_metrics until it is resolved.
CREATE TABLE IF NOT EXISTS pixamind.projection_failures (
    id          BIGSERIAL PRIMARY KEY,
    projection  TEXT NOT NULL,
    first_block INT NOT NULL,
    last_block  INT NOT NULL,
    sqlstate    TEXT,
    error       TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS projection_failures_open_idx
    ON pixamind.projection_failures (projection, first_block) WHERE resolved_at IS NULL;

-- Social event outbox (events contract v1, docs/pixamind/EVENTS.md).
-- (op_id, seq) is the event id: op_id is the HAF operation id, which encodes
-- the block number and the position in the block, so event ids follow chain
-- order and are identical on every node and after every resync.
CREATE TABLE IF NOT EXISTS pixamind.events (
    op_id      BIGINT NOT NULL,
    seq        SMALLINT NOT NULL,
    block_num  INT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    type       TEXT NOT NULL,
    account    VARCHAR(16) COLLATE "C",   -- who acted
    target     VARCHAR(16) COLLATE "C",   -- who is affected (post author, followed account, ...)
    payload    JSONB NOT NULL,
    version    SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (op_id, seq)
);
CREATE INDEX IF NOT EXISTS events_block_num_idx ON pixamind.events (block_num);
CREATE INDEX IF NOT EXISTS events_type_op_id_idx ON pixamind.events (type, op_id, seq);
CREATE INDEX IF NOT EXISTS events_account_op_id_idx ON pixamind.events (account, op_id, seq);
CREATE INDEX IF NOT EXISTS events_target_op_id_idx ON pixamind.events (target, op_id, seq) WHERE target IS NOT NULL;

-- Paid post promotion: a transfer of the stable token (PXS on Pixa) to @null
-- whose memo is "@author/permlink". The funds are burned by the chain; this
-- is the payment log the promoted feed is computed from.
CREATE TABLE IF NOT EXISTS pixamind.post_promotions (
    op_id      BIGINT PRIMARY KEY,
    block_num  INT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    sender     VARCHAR(16) COLLATE "C" NOT NULL,
    author     VARCHAR(16) COLLATE "C" NOT NULL,
    permlink   VARCHAR(255) COLLATE "C" NOT NULL,
    post_id    INT,                       -- hivemind_app.hive_posts.id; NULL when no such post exists
    amount     NUMERIC(12, 3) NOT NULL
);
CREATE INDEX IF NOT EXISTS post_promotions_block_num_idx ON pixamind.post_promotions (block_num);
CREATE INDEX IF NOT EXISTS post_promotions_post_id_idx ON pixamind.post_promotions (post_id);
CREATE INDEX IF NOT EXISTS post_promotions_sender_idx ON pixamind.post_promotions (sender, op_id);

-- Total promotion per post, recomputed from post_promotions for every post a batch touches.
CREATE TABLE IF NOT EXISTS pixamind.promoted_posts (
    post_id          INT PRIMARY KEY,
    promoted         NUMERIC(12, 3) NOT NULL,
    last_promoted_at TIMESTAMP NOT NULL,
    last_block       INT NOT NULL
);
CREATE INDEX IF NOT EXISTS promoted_posts_promoted_idx ON pixamind.promoted_posts (promoted DESC, post_id DESC);

-- Operations loaded straight from HAF to (re)build Pixamind projections for a
-- block range outside the sync loop (`pixamind events backfill`). Same shape as
-- hivemind_app._ops_staging.
CREATE UNLOGGED TABLE IF NOT EXISTS pixamind._backfill_ops (
    id         BIGINT NOT NULL,
    block_num  INT NOT NULL,
    block_date TIMESTAMP NOT NULL,
    op_type_id SMALLINT NOT NULL,
    val        JSONB
);
