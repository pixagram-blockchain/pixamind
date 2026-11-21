-- Pixamind runtime: the shared feeds cache (tables in migrations/0002_feed_cache.sql).
--
-- After each live block commits, the indexer calls pixamind.refresh_feed_cache()
-- in a transaction of its own: every enabled target of pixamind.feed_cache_targets
-- is computed once with the same dispatch() the API uses, and the row is tagged
-- with the block the hivemind_app context is at. hivemind_endpoints.home asks
-- hivemind_postgrest_utilities.cached_result() before dispatching; a row is
-- returned only if all of these hold, otherwise the API computes as usual:
--   * same api, method and params (the same jsonb: key order and spacing do not
--     matter, number spelling does: 20 is not 20.0),
--   * the context is still at the row's block (nothing changed since),
--   * the session's hivemind_app.max_posts_per_call is the one it was computed with,
--   * the request did not fail (errors are always computed, they carry a timestamp),
--   * the server is not a standby (unlogged tables cannot be read there).
-- The cached result is the text dispatch() returned, so the response is
-- byte-identical. The block is visible as soon as it commits; between that commit
-- and the refresh, the API simply computes these feeds itself.

-- The block the hivemind_app context is at, read the way get_sync_status does.
CREATE OR REPLACE FUNCTION pixamind.feed_cache_block()
RETURNS INT LANGUAGE sql STABLE AS
$$ SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app' $$;

CREATE OR REPLACE FUNCTION pixamind.feed_cache_session_limit()
RETURNS TEXT LANGUAGE sql STABLE AS
$$ SELECT COALESCE(current_setting('hivemind_app.max_posts_per_call', true), '') $$;

-- Replaces the no-op default defined next to hivemind_endpoints.home (home.sql).
-- On the hot path of every API call: one primary-key probe, and it never raises.
CREATE OR REPLACE FUNCTION hivemind_postgrest_utilities.cached_result(_api TEXT, _method TEXT, _params JSONB)
RETURNS JSON LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _result JSON;
BEGIN
  IF _params IS NULL OR pg_is_in_recovery() THEN
    RETURN NULL;
  END IF;
  -- params: jsonb equality finds the row, the text comparison makes the match exact
  -- (20 and 20.0 are equal jsonb, but ->> tells them apart)
  SELECT c.result INTO _result
    FROM pixamind.feed_cache c
   WHERE c.api = _api AND c.method = _method AND c.params = _params AND c.params::TEXT = _params::TEXT
     AND c.block_num = (SELECT hc.current_block_num FROM hafd.contexts hc WHERE hc.name = 'hivemind_app')
     AND c.session_limit = COALESCE(current_setting('hivemind_app.max_posts_per_call', true), '');
  RETURN _result;
END
$$;

-- Recompute every enabled target for the block the context is at. Called by the
-- indexer right after a live block's COMMIT (pixamind/indexer/hooks.py). An
-- unchanged result keeps its stored value and only moves to the new block. Each
-- target runs in its own subtransaction: one that fails is stored without a
-- result (so the API computes it) and the others carry on. Skipped while the
-- indexer catches up (more than _max_pending irreversible blocks waiting): the
-- next block would follow within milliseconds and nobody would be served these.
CREATE OR REPLACE FUNCTION pixamind.refresh_feed_cache(_max_pending INT DEFAULT 20)
RETURNS JSONB LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _block INT := pixamind.feed_cache_block();
  _limit TEXT := pixamind.feed_cache_session_limit();
  _pending INT;
  _target RECORD;
  _text TEXT;
  _hash TEXT;
  _changed INT := 0;
  _unchanged INT := 0;
  _failed INT := 0;
BEGIN
  IF COALESCE(_block, 0) <= 0 THEN
    RETURN jsonb_build_object('block', 0, 'changed', 0, 'unchanged', 0, 'failed', 0);
  END IF;
  BEGIN
    _pending := hive.app_get_irreversible_block() - _block;
  EXCEPTION WHEN OTHERS THEN
    _pending := NULL;  -- unknown: refresh
  END;
  IF _pending > _max_pending THEN
    RETURN jsonb_build_object('block', _block, 'skipped', 'catching_up', 'pending_blocks', _pending);
  END IF;

  -- rows of targets that were removed or disabled
  DELETE FROM pixamind.feed_cache c
   WHERE NOT EXISTS (SELECT 1 FROM pixamind.feed_cache_targets t
                      WHERE t.enabled AND t.api = c.api AND t.method = c.method AND t.params = c.params);

  FOR _target IN SELECT api, method, params FROM pixamind.feed_cache_targets WHERE enabled ORDER BY id LOOP
    BEGIN
      PERFORM set_config('response.headers', '', true);
      PERFORM set_config('response.status', '', true);
      _text := hivemind_postgrest_utilities.dispatch(_target.api, _target.method, _target.params)::TEXT;
      -- an answer that sets HTTP headers or a status cannot be replayed from a row
      IF current_setting('response.headers', true) <> '' OR current_setting('response.status', true) <> '' THEN
        RAISE EXCEPTION 'not cacheable: the method sets HTTP response headers or status';
      END IF;
      _hash := md5(_text);
      UPDATE pixamind.feed_cache
         SET block_num = _block
       WHERE api = _target.api AND method = _target.method AND params = _target.params
         AND hash = _hash AND session_limit = _limit;
      IF FOUND THEN
        _unchanged := _unchanged + 1;
      ELSE
        INSERT INTO pixamind.feed_cache (api, method, params, block_num, session_limit, result, hash, changed_block, error)
        VALUES (_target.api, _target.method, _target.params, _block, _limit, _text::JSON, _hash, _block, NULL)
        ON CONFLICT (api, method, params) DO UPDATE
          SET block_num = EXCLUDED.block_num, session_limit = EXCLUDED.session_limit, result = EXCLUDED.result,
              hash = EXCLUDED.hash, changed_block = EXCLUDED.changed_block, error = NULL;
        _changed := _changed + 1;
      END IF;
    EXCEPTION WHEN OTHERS THEN
      INSERT INTO pixamind.feed_cache (api, method, params, block_num, session_limit, result, hash, changed_block, error)
      VALUES (_target.api, _target.method, _target.params, _block, _limit, NULL, NULL, _block, left(SQLERRM, 500))
      ON CONFLICT (api, method, params) DO UPDATE
        SET block_num = EXCLUDED.block_num, session_limit = EXCLUDED.session_limit, result = NULL, hash = NULL,
            changed_block = EXCLUDED.changed_block, error = EXCLUDED.error;
      _failed := _failed + 1;
    END;
  END LOOP;

  RETURN jsonb_build_object('block', _block, 'changed', _changed, 'unchanged', _unchanged, 'failed', _failed);
END
$$;

-- Forget every cached result (the indexer does it when it starts, and when the
-- cache is turned off). Run it after editing indexed data by hand.
CREATE OR REPLACE FUNCTION pixamind.clear_feed_cache()
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _n INT;
BEGIN
  DELETE FROM pixamind.feed_cache;
  GET DIAGNOSTICS _n = ROW_COUNT;
  RETURN _n;
END
$$;

-- For pixamind_api.get_info.
CREATE OR REPLACE FUNCTION pixamind.feed_cache_state()
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _block INT := pixamind.feed_cache_block();
  _limit TEXT := pixamind.feed_cache_session_limit();
  _state JSONB := jsonb_build_object(
    'enabled', COALESCE(pixamind.get_setting('feed_cache', 'false')::BOOLEAN, FALSE),
    'targets', (SELECT count(*) FROM pixamind.feed_cache_targets WHERE enabled));
BEGIN
  IF pg_is_in_recovery() THEN
    RETURN _state || jsonb_build_object('standby', TRUE);
  END IF;
  RETURN _state || (
    SELECT jsonb_build_object(
      'fresh', count(*) FILTER (WHERE c.block_num = _block AND c.session_limit = _limit AND c.result IS NOT NULL),
      'failing', count(*) FILTER (WHERE c.block_num = _block AND c.result IS NULL),
      'block', max(c.block_num),
      'size_bytes', pg_total_relation_size('pixamind.feed_cache'))
    FROM pixamind.feed_cache c);
END
$$;
