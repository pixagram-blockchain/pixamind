-- Pixamind: the result the indexer already computed for a request at the current
-- block (shared feeds cache), or NULL to compute it. This default caches nothing;
-- pixamind/sql/runtime/90_feed_cache.sql replaces it once the Pixamind schema is
-- installed, so the API keeps working, uncached, if that schema is missing.
CREATE OR REPLACE FUNCTION hivemind_postgrest_utilities.cached_result(_api TEXT, _method TEXT, _params JSONB)
RETURNS JSON
LANGUAGE sql
STABLE
AS
$$
  SELECT NULL::JSON
$$
;

DROP FUNCTION IF EXISTS hivemind_endpoints.home(JSON);
CREATE FUNCTION hivemind_endpoints.home(JSON)
RETURNS JSON
LANGUAGE 'plpgsql'
STABLE
-- Pixamind: no JIT for API calls, whatever the database or role settings (the
-- database-level jit=off of install_app.sql is lost when a dump is restored into
-- a new database). LLVM compilation costs more than it saves on these queries.
SET jit = off
AS
$$
DECLARE
  __request_data JSON = $1;
  __id TEXT;
  __jsonrpc TEXT;
  __method TEXT;
  __params JSON;
  __api_type TEXT;
  __method_type TEXT;
  __params_jsonb JSONB;
  __cached JSON;
BEGIN

  __jsonrpc = __request_data->>'jsonrpc';
  IF __jsonrpc != '2.0' THEN
    RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_invalid_json_format_exception('Invalid JSON-RPC');
  END IF;

  __id = __request_data->>'id';
  IF __id IS NULL THEN
    return json_build_object('id', null, 'jsonrpc', '2.0', 'error', 'id required');
  END IF;

  __method = __request_data->>'method';
  if __method is NULL THEN
    RETURN json_build_object('id', __id, 'jsonrpc', '2.0', 'error', 'no method passed');
  END IF;

  --early check to reject methods that require parameters
  __params = __request_data->'params';
  IF __method NOT IN ('call', 'hive.db_head_state', 'condenser.get_trending_tags',
                      'bridge.list_pop_communities', 'bridge.get_payout_stats',
                      'bridge.get_trending_topics','bridge.list_muted_reasons_enum',
                      'pixamind_api.get_info', 'pixamind_api.get_metrics', 'pixamind_api.get_events',
                      'pixamind_feed.get_promoted_posts'
                     ) THEN
    IF __params is NULL THEN
      RETURN json_build_object('id', __id, 'jsonrpc', '2.0', 'error', 'this method requires parameters');
    END IF;
  END IF;

  __params_jsonb = __params::JSONB;
  --handle 'call' method (probably should remove this if condition, no one uses and it is inefficiently implemented)
  if lower(__method) = 'call' THEN
    if jsonb_array_length(__params_jsonb) < 2 THEN
      RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_invalid_json_format_exception('Invalid JSON-RPC');
    END IF;
    __api_type = __params_jsonb->>0;
    __method_type = __params_jsonb->>1;
    -- this 'call' keyword in test cases gives another error messages, so it is important
    __params_jsonb = jsonb_build_object('used_call_keyword', True, 'params', __params_jsonb->2);
  ELSE
    SELECT split_part(__method, '.', 1) INTO __api_type;
    SELECT split_part(__method, '.', 2) INTO __method_type;
  END IF;

  -- Pixamind: shared feeds are computed once per block by the indexer; same bytes
  -- as dispatch() below would produce (docs/pixamind/FEED-CACHE.md)
  __cached = hivemind_postgrest_utilities.cached_result(__api_type, __method_type, __params_jsonb);
  IF __cached IS NOT NULL THEN
    RETURN json_build_object( 'id', __id, 'jsonrpc', '2.0', 'result', __cached );
  END IF;

  RETURN json_build_object( 'id', __id, 'jsonrpc', '2.0', 'result', hivemind_postgrest_utilities.dispatch(__api_type, __method_type, __params_jsonb) );

  EXCEPTION
    WHEN raise_exception THEN
      DECLARE
        error_obj JSON;
        error_parts JSON;
      BEGIN
        error_parts := SQLERRM::JSON;
        error_obj := json_build_object(
          'code', (error_parts->>'code')::INT,
          'message', error_parts->>'message',
          'data', error_parts->'data'
        );
        RETURN json_build_object('id', __id, 'jsonrpc', '2.0', 'error', error_obj);
      END;
END
$$
;

/*
DROP FUNCTION IF EXISTS hivemind_endpoints.home_test;
CREATE FUNCTION hivemind_endpoints.home_test(json_rpc JSON)
RETURNS JSONB
LANGUAGE 'plpgsql'
STABLE
AS
$$
DECLARE
  __request_data JSON = $1;
  __jsonrpc TEXT;
  __method TEXT;
  __params JSONB;
  __id JSONB;

  __result JSONB;
BEGIN
  __jsonrpc = (__request_data->>'jsonrpc');
  __method = (__request_data->>'method');
  __params = (__request_data->'params');
  __id = (__request_data->'id');

  SELECT hivemind_postgrest_utilities.check_general_json_format(__jsonrpc, __method, __params, __id) INTO __result;

  RETURN jsonb_build_object(
    'jsonrpc', '2.0',
    'id', __id,
    'result', hivemind_postgrest_utilities.dispatch(
                                                    __result->>'api_type',
                                                    __result->>'method_type',
                                                    (__result->'json_with_params_is_object')::BOOLEAN,
                                                    __result->'params'
    )
  );

  EXCEPTION
    WHEN raise_exception THEN
      RETURN jsonb_build_object(
        'jsonrpc', '2.0',
        'error', SQLERRM::JSONB,
        'id', __id
      );
END
$$
;
*/
