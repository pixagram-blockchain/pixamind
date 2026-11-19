-- Minimal stand-in for the HAF (hive_fork_manager) and reputation_tracker objects
-- that the hivemind/Pixamind SQL references, so the real runtime SQL can be
-- installed and exercised on a plain PostgreSQL server (no hived, no HAF build).
--
-- Only the surface used by the SQL in this repository is modelled. Context state
-- lives in hafd.contexts; tests move it with pixamind_test.set_context(...).
-- This file is test infrastructure: it is never installed on a real HAF node.

CREATE SCHEMA IF NOT EXISTS hafd;
CREATE SCHEMA IF NOT EXISTS hive;
CREATE SCHEMA IF NOT EXISTS reptracker_app;
CREATE SCHEMA IF NOT EXISTS reptracker_endpoints;
CREATE SCHEMA IF NOT EXISTS pixamind_test;

CREATE DOMAIN hafd.context_name AS TEXT;
CREATE DOMAIN hive.context_name AS TEXT;
CREATE DOMAIN hive.contexts_group AS TEXT[];
CREATE TYPE hive.blocks_range AS (first_block INT, last_block INT);

-- ---------------------------------------------------------------- HAF state
CREATE TABLE hafd.hive_state (consistent_block INT NOT NULL DEFAULT 0);
INSERT INTO hafd.hive_state VALUES (0);

CREATE TABLE hafd.contexts (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    schema TEXT NOT NULL,
    current_block_num INT NOT NULL DEFAULT 0,
    irreversible_block INT NOT NULL DEFAULT 0,
    fork_id BIGINT NOT NULL DEFAULT 1,
    is_forking BOOLEAN NOT NULL DEFAULT TRUE,
    stage TEXT NOT NULL DEFAULT 'live'
);
INSERT INTO hafd.contexts (name, schema) VALUES ('hivemind_app', 'hivemind_app');

CREATE TABLE hafd.registered_tables (context_id INT, origin_table_schema TEXT, origin_table_name TEXT);

CREATE TABLE hafd.blocks (
    num INT PRIMARY KEY,
    hash BYTEA,
    prev BYTEA,
    created_at TIMESTAMP NOT NULL,
    producer_account_id INT
);
CREATE TABLE hafd.blocks_reversible (LIKE hafd.blocks);
ALTER TABLE hafd.blocks_reversible ADD COLUMN fork_id BIGINT NOT NULL DEFAULT 1;

CREATE TABLE hafd.accounts (id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, block_num INT);

CREATE TABLE hafd.custom_json_types (id SERIAL PRIMARY KEY, custom_json_id TEXT UNIQUE NOT NULL);
INSERT INTO hafd.custom_json_types (custom_json_id) VALUES ('follow'), ('reblog'), ('community'), ('notify');

CREATE TABLE hafd.operations (
    id BIGINT PRIMARY KEY,
    block_num INT NOT NULL,
    trx_in_block SMALLINT NOT NULL DEFAULT 0,
    op_pos INT NOT NULL DEFAULT 0,
    op_type_id SMALLINT NOT NULL,
    body_value JSONB,
    custom_json_type_id INT
);

-- operation ids: block_num in the high 32 bits, position in block next (HAF layout)
CREATE FUNCTION hafd.operation_id(_block_num INT, _pos_in_block INT)
RETURNS TABLE (operation_id BIGINT) LANGUAGE sql IMMUTABLE AS
$$ SELECT (_block_num::BIGINT << 32) | (_pos_in_block::BIGINT << 8) $$;

-- ------------------------------------------- global views (context views: haf_context_views.sql)
CREATE VIEW hive.blocks_view AS SELECT num, hash, prev, created_at, producer_account_id FROM hafd.blocks;
CREATE VIEW hive.accounts_view AS SELECT id, name, block_num FROM hafd.accounts;

-- ------------------------------------------------------------ HAF functions
CREATE FUNCTION hive.app_get_current_block_num(_context hafd.context_name)
RETURNS INT LANGUAGE sql STABLE AS
$$ SELECT current_block_num FROM hafd.contexts WHERE name = _context $$;

CREATE FUNCTION hive.app_get_current_block_num(_contexts hive.contexts_group)
RETURNS INT LANGUAGE sql STABLE AS
$$ SELECT min(current_block_num) FROM hafd.contexts WHERE name = ANY(_contexts) $$;

CREATE FUNCTION hive.app_get_irreversible_block()
RETURNS INT LANGUAGE sql STABLE AS $$ SELECT consistent_block FROM hafd.hive_state $$;

CREATE FUNCTION hive.app_get_irreversible_block(_context hafd.context_name)
RETURNS INT LANGUAGE sql STABLE AS
$$ SELECT irreversible_block FROM hafd.contexts WHERE name = _context $$;

CREATE FUNCTION hive.app_is_forking(_context hafd.context_name)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$ SELECT is_forking FROM hafd.contexts WHERE name = _context $$;

CREATE FUNCTION hive.get_current_stage_name(_context hafd.context_name)
RETURNS TEXT LANGUAGE sql STABLE AS $$ SELECT stage FROM hafd.contexts WHERE name = _context $$;

CREATE FUNCTION hive.is_instance_ready() RETURNS BOOLEAN LANGUAGE sql STABLE AS $$ SELECT TRUE $$;

CREATE FUNCTION hive.get_estimated_hive_head_block()
RETURNS INT LANGUAGE sql STABLE AS $$ SELECT consistent_block + 2 FROM hafd.hive_state $$;

CREATE FUNCTION hive.get_app_current_block_age(_contexts hive.contexts_group)
RETURNS INTERVAL LANGUAGE sql STABLE AS
$$
  SELECT now() - min(coalesce(b.created_at, to_timestamp(0)::timestamp))
  FROM unnest(_contexts) AS c(name)
  LEFT JOIN hafd.contexts hc USING (name)
  LEFT JOIN hafd.blocks b ON b.num = hc.current_block_num
$$;

CREATE FUNCTION hive.convert_to_blocks_range(_from INT, _to INT)
RETURNS hive.blocks_range LANGUAGE sql IMMUTABLE AS $$ SELECT ROW(_from, _to)::hive.blocks_range $$;

CREATE TYPE hive.application_stage AS (name TEXT, min_head_block_distance INT, blocks_limit_in_group INT, sleep INTERVAL);
CREATE FUNCTION hive.stage(_name TEXT, _distance INT, _limit INT, _sleep INTERVAL)
RETURNS hive.application_stage LANGUAGE sql IMMUTABLE AS $$ SELECT ROW(_name, _distance, _limit, _sleep)::hive.application_stage $$;
CREATE FUNCTION hive.live_stage()
RETURNS hive.application_stage LANGUAGE sql IMMUTABLE AS $$ SELECT ROW('live', 0, 1, '1 second'::interval)::hive.application_stage $$;

-- ---------------------------------------------------------- reputation_tracker
CREATE TABLE reptracker_app.account_reputations (
    account_id INT PRIMARY KEY,
    reputation BIGINT NOT NULL DEFAULT 0,
    is_implicit BOOLEAN NOT NULL DEFAULT TRUE
);

-- ------------------------------------------------------------- test helpers
-- Move the hivemind_app context (processed block, irreversible block, stage).
CREATE FUNCTION pixamind_test.set_context(_current INT, _irreversible INT, _stage TEXT DEFAULT 'live')
RETURNS VOID LANGUAGE sql AS
$$
  UPDATE hafd.contexts SET current_block_num = _current, irreversible_block = _irreversible, stage = _stage
  WHERE name = 'hivemind_app';
  UPDATE hafd.hive_state SET consistent_block = _irreversible;
$$;

-- Add a block; 3-second spacing from a fixed genesis keeps timestamps deterministic.
CREATE FUNCTION pixamind_test.add_block(_num INT)
RETURNS VOID LANGUAGE sql AS
$$
  INSERT INTO hafd.blocks (num, created_at)
  VALUES (_num, TIMESTAMP '2026-09-01 00:00:00' + make_interval(secs => _num * 3))
  ON CONFLICT (num) DO NOTHING;
$$;

-- Append an operation to a block, at the next position; returns its id.
CREATE FUNCTION pixamind_test.add_op(_block INT, _op_type SMALLINT, _body JSONB)
RETURNS BIGINT LANGUAGE plpgsql AS
$$
DECLARE
  _pos INT;
  _id BIGINT;
  _cj INT;
BEGIN
  PERFORM pixamind_test.add_block(_block);
  SELECT count(*) + 1 INTO _pos FROM hafd.operations WHERE block_num = _block;
  SELECT operation_id INTO _id FROM hafd.operation_id(_block, _pos);
  _id := _id | _op_type;
  IF _op_type = 18 THEN
    INSERT INTO hafd.custom_json_types (custom_json_id) VALUES (_body->>'id') ON CONFLICT DO NOTHING;
    SELECT id INTO _cj FROM hafd.custom_json_types WHERE custom_json_id = _body->>'id';
  END IF;
  INSERT INTO hafd.operations (id, block_num, op_pos, op_type_id, body_value, custom_json_type_id)
  VALUES (_id, _block, _pos, _op_type, _body, _cj);
  RETURN _id;
END
$$;
