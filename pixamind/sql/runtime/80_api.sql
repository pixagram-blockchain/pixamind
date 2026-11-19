-- Pixamind runtime: JSON-RPC methods of the Pixamind namespaces.
--
--   pixamind_api.get_info                   node, schema, chain and feature description
--   pixamind_api.get_metrics                indexer lag, throughput, block->API latency, failures
--   pixamind_api.get_events                 the social event outbox, cursor-paged
--   pixamind_content.get_content_metadata   artwork/post metadata (format, size, licence, AI disclosure)
--   pixamind_graph.get_mutual_follows       accounts an account follows that follow it back
--   pixamind_graph.get_relationships        one observer against up to 100 accounts, in one call
--   pixamind_feed.get_promoted_posts        the promoted feed (same as bridge.get_ranked_posts sort=promoted)
--   pixamind_feed.get_promotions            promotion payments of a post or of a sender
--
-- Requests arrive through hivemind_endpoints.home -> hivemind_postgrest_utilities.dispatch,
-- so errors use the same JSON-RPC error objects as the inherited APIs.

CREATE OR REPLACE FUNCTION pixamind.raise_error(_code INT, _message TEXT)
RETURNS JSONB LANGUAGE plpgsql IMMUTABLE AS
$$
BEGIN
  RETURN hivemind_postgrest_utilities.raise_exception(_code, _message);
END
$$;

-- Processed and irreversible blocks of the hivemind_app context, read the way
-- hivemind_endpoints.get_sync_status does (no HAF view involved).
CREATE OR REPLACE FUNCTION pixamind.context_state()
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _processed INT := (SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app');
  _irreversible INT;
  _stage TEXT;
  _forking BOOLEAN;
  _age DOUBLE PRECISION;
  _head INT;
BEGIN
  BEGIN _irreversible := hive.app_get_irreversible_block('hivemind_app'); EXCEPTION WHEN OTHERS THEN NULL; END;
  BEGIN _stage := hive.get_current_stage_name('hivemind_app'); EXCEPTION WHEN OTHERS THEN NULL; END;
  BEGIN _forking := hive.app_is_forking('hivemind_app'); EXCEPTION WHEN OTHERS THEN NULL; END;
  BEGIN _head := hive.get_estimated_hive_head_block(); EXCEPTION WHEN OTHERS THEN NULL; END;
  IF COALESCE(_processed, 0) > 0 THEN
    BEGIN
      IF hive.is_instance_ready() THEN
        _age := EXTRACT(EPOCH FROM hive.get_app_current_block_age(ARRAY['hivemind_app']::hive.contexts_group));
      END IF;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
  RETURN jsonb_strip_nulls(jsonb_build_object(
    'processed_block', COALESCE(_processed, 0),
    'irreversible_block', _irreversible,
    'estimated_head_block', _head,
    'stage', _stage,
    'forking', _forking,
    'processed_block_age_seconds', round(_age::NUMERIC, 3)
  ));
END
$$;

CREATE OR REPLACE FUNCTION pixamind.event_types()
RETURNS JSONB LANGUAGE sql IMMUTABLE AS
$$
  SELECT '["vote", "post_created", "post_updated", "comment_created", "comment_updated", "post_deleted",
           "follow", "reblog", "reblog_deleted", "community", "account_created", "account_updated",
           "author_reward", "promotion"]'::JSONB
$$;

-- ------------------------------------------------------------ pixamind_api
CREATE OR REPLACE FUNCTION pixamind.api_get_info(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _migration INT := (SELECT max(version) FROM pixamind.schema_migrations);
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params, '{}', 0, NULL);
  RETURN jsonb_build_object(
    'name', 'pixamind',
    'version', pixamind.get_setting('version', ''),
    'hivemind_base', pixamind.get_setting('hivemind_base', ''),
    'git_revision', pixamind.get_setting('git_revision', ''),
    'chain', jsonb_build_object(
      'name', pixamind.get_setting('chain', ''),
      'liquid_symbol', '{{HIVE_SYMBOL}}', 'stable_symbol', '{{HBD_SYMBOL}}', 'vests_symbol', '{{VESTS_SYMBOL}}'),
    'database', jsonb_build_object(
      'app_schema', 'hivemind_app', 'endpoints_schema', 'hivemind_endpoints',
      'pixamind_schema', 'pixamind', 'pixamind_migration', _migration),
    'context', pixamind.context_state(),
    'features', jsonb_build_object(
      'events', pixamind.get_setting('events', 'false')::BOOLEAN,
      'events_keep_blocks', pixamind.get_setting('events_keep_blocks', '0')::INT,
      'promotions', pixamind.get_setting('promotions', 'true')::BOOLEAN,
      'telemetry', pixamind.get_setting('telemetry', 'true')::BOOLEAN),
    'event_types', pixamind.event_types(),
    'namespaces', '["pixamind_api", "pixamind_content", "pixamind_graph", "pixamind_feed"]'::JSONB
  );
END
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_metrics(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _window INT;
  _ctx JSONB := pixamind.context_state();
  _processed INT := (_ctx->>'processed_block')::INT;
  _irreversible INT := (_ctx->>'irreversible_block')::INT;
  _head INT := (_ctx->>'estimated_head_block')::INT;
  _since TIMESTAMPTZ;
  _throughput JSONB;
  _latency JSONB;
  _phases JSONB;
  _last JSONB;
  _events JSONB;
  _failures JSONB;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params, '{"window_minutes": "number"}', 0, NULL);
  _window := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'window_minutes', False), 10, 1, 1440, 'window_minutes');
  _since := clock_timestamp() - make_interval(mins => _window);

  SELECT jsonb_build_object(
           'batches', count(*),
           'blocks', COALESCE(sum(b.last_block - b.first_block + 1), 0),
           'ops', COALESCE(sum(b.ops), 0),
           'busy_seconds', round(COALESCE(sum(b.seconds), 0)::NUMERIC, 3),
           -- blocks applied per wall-clock second over the window
           'blocks_per_second', round(COALESCE(sum(b.last_block - b.first_block + 1), 0)::NUMERIC / (_window * 60), 3),
           -- what the indexer could sustain: blocks per busy second
           'capacity_blocks_per_second', CASE WHEN sum(b.seconds) > 0
                THEN round((sum(b.last_block - b.first_block + 1) / sum(b.seconds))::NUMERIC, 3) END,
           'ops_per_second', round(COALESCE(sum(b.ops), 0)::NUMERIC / (_window * 60), 3))
  INTO _throughput
  FROM pixamind.sync_batches b
  WHERE b.recorded_at >= _since;

  SELECT jsonb_build_object(
           'samples', count(*),
           'p50', round(percentile_cont(0.5) WITHIN GROUP (ORDER BY b.latency_seconds)::NUMERIC, 3),
           'p95', round(percentile_cont(0.95) WITHIN GROUP (ORDER BY b.latency_seconds)::NUMERIC, 3),
           'p99', round(percentile_cont(0.99) WITHIN GROUP (ORDER BY b.latency_seconds)::NUMERIC, 3),
           'max', round(max(b.latency_seconds)::NUMERIC, 3))
  INTO _latency
  FROM pixamind.sync_batches b
  WHERE b.recorded_at >= _since AND b.mode = 'live' AND b.latency_seconds IS NOT NULL;

  SELECT COALESCE(jsonb_object_agg(x.phase, x.avg_seconds), '{}'::JSONB) INTO _phases
  FROM (
    SELECT p.key AS phase, round(avg(p.value::NUMERIC), 4) AS avg_seconds
    FROM pixamind.sync_batches b, jsonb_each_text(b.phases) p
    WHERE b.recorded_at >= _since AND p.value ~ '^-?[0-9.]+$'
    GROUP BY p.key
  ) x;

  SELECT jsonb_build_object('mode', b.mode, 'first_block', b.first_block, 'last_block', b.last_block,
                            'block_time', b.block_time, 'recorded_at', b.recorded_at, 'ops', b.ops,
                            'seconds', round(b.seconds::NUMERIC, 4),
                            'latency_seconds', round(b.latency_seconds::NUMERIC, 3), 'phases', b.phases)
  INTO _last
  FROM pixamind.sync_batches b ORDER BY b.id DESC LIMIT 1;

  SELECT jsonb_build_object(
           'enabled', pixamind.get_setting('events', 'false')::BOOLEAN,
           'oldest_block', (SELECT min(block_num) FROM pixamind.events),
           'newest_block', (SELECT max(block_num) FROM pixamind.events),
           'estimated_count', GREATEST((SELECT reltuples::BIGINT FROM pg_class WHERE oid = 'pixamind.events'::REGCLASS), 0))
  INTO _events;

  SELECT jsonb_build_object(
           'open', (SELECT count(*) FROM pixamind.projection_failures WHERE resolved_at IS NULL),
           'latest', COALESCE((
              SELECT jsonb_agg(jsonb_build_object('projection', f.projection, 'first_block', f.first_block,
                                                  'last_block', f.last_block, 'error', f.error,
                                                  'recorded_at', f.recorded_at) ORDER BY f.id DESC)
              FROM (SELECT * FROM pixamind.projection_failures WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 5) f
           ), '[]'::JSONB))
  INTO _failures;

  RETURN jsonb_build_object(
    'context', _ctx,
    'lag', jsonb_strip_nulls(jsonb_build_object(
       'blocks_behind_irreversible', GREATEST(_irreversible - _processed, 0),
       'reversible_blocks_processed', GREATEST(_processed - _irreversible, 0),
       'blocks_behind_head', GREATEST(_head - _processed, 0))),
    'window_minutes', _window,
    'throughput', _throughput,
    'live_latency_seconds', _latency,
    'phase_seconds_avg', _phases,
    'last_batch', _last,
    'events', _events,
    'promoted_posts', (SELECT count(*) FROM pixamind.promoted_posts),
    'failures', _failures
  );
END
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_events(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _cursor RECORD;
  _from_block INT;
  _limit INT;
  _types TEXT[];
  _accounts TEXT[];
  _irreversible_only BOOLEAN;
  _processed INT := COALESCE((SELECT current_block_num FROM hafd.contexts WHERE name = 'hivemind_app'), 0);
  _irreversible INT;
  _upto INT;
  _events JSONB;
  _count INT;
  _next TEXT;
  _oldest RECORD;
BEGIN
  IF NOT pixamind.get_setting('events', 'false')::BOOLEAN THEN
    PERFORM pixamind.raise_error(-32000, 'the event outbox is not enabled on this node (start the indexer with --pixamind-events)');
  END IF;

  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"cursor": "string", "from_block": "number", "limit": "number", "types": "array", "accounts": "array", "irreversible_only": "boolean"}',
    0, NULL);
  _cursor := pixamind.parse_event_cursor(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'cursor', False));
  _from_block := hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'from_block', False);
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 100, 1, 1000, 'limit');
  _irreversible_only := COALESCE((_params->>'irreversible_only')::BOOLEAN, TRUE);

  IF _params ? 'types' AND jsonb_typeof(_params->'types') = 'array' THEN
    SELECT array_agg(value) INTO _types FROM jsonb_array_elements_text(_params->'types');
    IF EXISTS (SELECT 1 FROM unnest(_types) t WHERE NOT pixamind.event_types() ? t) THEN
      PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception(
        'unknown event type; known types: ' || (SELECT string_agg(value, ', ') FROM jsonb_array_elements_text(pixamind.event_types())));
    END IF;
  END IF;
  IF _params ? 'accounts' AND jsonb_typeof(_params->'accounts') = 'array' THEN
    SELECT array_agg(value) INTO _accounts FROM jsonb_array_elements_text(_params->'accounts');
    IF cardinality(_accounts) > 100 THEN
      PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('at most 100 accounts');
    END IF;
    PERFORM hivemind_postgrest_utilities.valid_accounts(_accounts);
  END IF;

  BEGIN
    _irreversible := hive.app_get_irreversible_block('hivemind_app');
  EXCEPTION WHEN OTHERS THEN
    _irreversible := NULL;
  END;
  _upto := CASE WHEN _irreversible_only THEN LEAST(_processed, COALESCE(_irreversible, 0)) ELSE _processed END;

  WITH page AS (
    SELECT e.*
    FROM pixamind.events e
    WHERE (_cursor.op_id IS NULL OR (e.op_id, e.seq) > (_cursor.op_id, _cursor.seq))
      AND (_from_block IS NULL OR e.block_num >= _from_block)
      AND e.block_num <= _upto
      AND (_types IS NULL OR e.type = ANY(_types))
      AND (_accounts IS NULL OR e.account = ANY(_accounts) OR e.target = ANY(_accounts))
    ORDER BY e.op_id, e.seq
    LIMIT _limit
  )
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
           'id', pixamind.event_cursor(p.op_id, p.seq),
           'block_num', p.block_num,
           'timestamp', to_char(p.created_at, 'YYYY-MM-DD"T"HH24:MI:SS'),
           'type', p.type,
           'account', p.account,
           'target', p.target,
           'data', p.payload,
           'version', p.version) ORDER BY p.op_id, p.seq), '[]'::JSONB),
         count(*),
         (array_agg(pixamind.event_cursor(p.op_id, p.seq) ORDER BY p.op_id DESC, p.seq DESC))[1]
  INTO _events, _count, _next
  FROM page p;

  SELECT e.op_id, e.seq, e.block_num INTO _oldest FROM pixamind.events e ORDER BY e.op_id, e.seq LIMIT 1;

  RETURN jsonb_build_object(
    'events', _events,
    'next_cursor', COALESCE(_next, pixamind.event_cursor(_cursor.op_id, _cursor.seq)),
    'has_more', _count = _limit,
    'upto_block', _upto,
    'processed_block', _processed,
    'irreversible_block', _irreversible,
    'retention', jsonb_build_object(
      'oldest_block', _oldest.block_num,
      'oldest_id', pixamind.event_cursor(_oldest.op_id, _oldest.seq),
      -- the cursor points before the oldest retained event: events may have been pruned in between
      'possible_gap', _cursor.op_id IS NOT NULL AND _oldest.op_id IS NOT NULL
                      AND (_cursor.op_id, _cursor.seq) < (_oldest.op_id, _oldest.seq))
  );
END
$$;

-- ------------------------------------------------------- pixamind_content
CREATE OR REPLACE FUNCTION pixamind.post_metadata(_author TEXT, _permlink TEXT)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT jsonb_strip_nulls(jsonb_build_object(
           'author', ha.name,
           'permlink', hpd.permlink,
           'depth', hp.depth,
           'category', cd.category,
           'community', hc.name,
           'created', to_char(hp.created_at, 'YYYY-MM-DD"T"HH24:MI:SS'),
           'updated', to_char(hp.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS'),
           'title', NULLIF(d.title, ''),
           'content', pixamind.content_metadata(d.json, left(d.body, 160), length(d.body))))
  FROM hivemind_app.hive_posts hp
  JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
  JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
  LEFT JOIN hivemind_app.hive_post_data d ON d.id = hp.id
  LEFT JOIN hivemind_app.hive_category_data cd ON cd.id = hp.category_id
  LEFT JOIN hivemind_app.hive_communities hc ON hc.id = hp.community_id
  WHERE ha.name = _author AND hpd.permlink = _permlink AND hp.counter_deleted = 0
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_content_metadata(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _author TEXT;
  _permlink TEXT;
  _result JSONB;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"author": "string", "permlink": "string", "posts": "array"}', 1, NULL);

  IF _params ? 'posts' AND jsonb_typeof(_params->'posts') = 'array' THEN
    IF jsonb_array_length(_params->'posts') > 100 THEN
      PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('at most 100 posts');
    END IF;
    SELECT COALESCE(jsonb_agg(
             CASE
               WHEN jsonb_typeof(p.value) = 'object' THEN pixamind.post_metadata(p.value->>'author', p.value->>'permlink')
               WHEN jsonb_typeof(p.value) = 'array' THEN pixamind.post_metadata(p.value->>0, p.value->>1)
             END ORDER BY p.ord), '[]'::JSONB)
    INTO _result
    FROM jsonb_array_elements(_params->'posts') WITH ORDINALITY AS p(value, ord);
    RETURN _result;
  END IF;

  _author := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'author', True));
  _permlink := hivemind_postgrest_utilities.valid_permlink(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'permlink', True));
  _result := pixamind.post_metadata(_author, _permlink);
  IF _result IS NULL THEN
    PERFORM hivemind_postgrest_utilities.raise_non_existing_post_exception(_author, _permlink);
  END IF;
  RETURN _result;
END
$$;

-- --------------------------------------------------------- pixamind_graph
CREATE OR REPLACE FUNCTION pixamind.api_get_mutual_follows(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _account_id INT;
  _start TEXT;
  _limit INT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"account": "string", "start": "string", "limit": "number"}', 1, NULL);
  _account_id := hivemind_postgrest_utilities.find_account_id(
    hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'account', True)), True);
  _start := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False), True);
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 100, 1, 1000, 'limit');

  RETURN COALESCE((
    SELECT jsonb_agg(m.name ORDER BY m.name)
    FROM (
      SELECT ha.name
      FROM hivemind_app.follows f
      JOIN hivemind_app.follows back ON back.follower = f.following AND back.following = f.follower
      JOIN hivemind_app.hive_accounts ha ON ha.id = f.following
      WHERE f.follower = _account_id
        AND (_start IS NULL OR _start = '' OR ha.name > _start)
      ORDER BY ha.name
      LIMIT _limit
    ) m
  ), '[]'::JSONB);
END
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_relationships(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _observer_id INT;
  _names TEXT[];
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"observer": "string", "accounts": "array"}', 2, NULL);
  _observer_id := hivemind_postgrest_utilities.find_account_id(
    hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'observer', True)), True);
  SELECT array_agg(value) INTO _names FROM jsonb_array_elements_text(_params->'accounts');
  IF _names IS NULL OR cardinality(_names) = 0 THEN
    RETURN '{}'::JSONB;
  END IF;
  IF cardinality(_names) > 100 THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('at most 100 accounts');
  END IF;
  PERFORM hivemind_postgrest_utilities.valid_accounts(_names);

  RETURN COALESCE((
    SELECT jsonb_object_agg(ha.name, jsonb_build_object(
      'follows', EXISTS (SELECT 1 FROM hivemind_app.follows x WHERE x.follower = _observer_id AND x.following = ha.id),
      'followed_by', EXISTS (SELECT 1 FROM hivemind_app.follows x WHERE x.follower = ha.id AND x.following = _observer_id),
      'ignores', EXISTS (SELECT 1 FROM hivemind_app.muted x WHERE x.follower = _observer_id AND x.following = ha.id),
      'blacklists', EXISTS (SELECT 1 FROM hivemind_app.blacklisted x WHERE x.follower = _observer_id AND x.following = ha.id),
      'follows_blacklists', EXISTS (SELECT 1 FROM hivemind_app.follow_blacklisted x WHERE x.follower = _observer_id AND x.following = ha.id),
      'follows_muted', EXISTS (SELECT 1 FROM hivemind_app.follow_muted x WHERE x.follower = _observer_id AND x.following = ha.id)))
    FROM hivemind_app.hive_accounts ha
    WHERE ha.name = ANY(_names)
  ), '{}'::JSONB);
END
$$;

-- ---------------------------------------------------------- pixamind_feed
-- Shared by bridge.get_ranked_posts (sort=promoted) and pixamind_feed.get_promoted_posts.
CREATE OR REPLACE FUNCTION pixamind.bridge_get_promoted_posts(
    _post_id INT, _observer_id INT, _limit INT, _tag TEXT, _muted_reasons_filter_mask INT
)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
BEGIN
  IF _tag = 'my' THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('promoted does not support tag `my`');
  END IF;
  RETURN pixamind.get_promoted_ranked_posts(_post_id, _observer_id, _limit, 0, _tag, True, _muted_reasons_filter_mask);
END
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_promoted_posts(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"start_author": "string", "start_permlink": "string", "limit": "number", "tag": "string", "observer": "string", "muted_reasons_filter": "array"}',
    0, NULL);
  RETURN hivemind_endpoints.bridge_api_get_ranked_posts(COALESCE(_params, '{}'::JSONB) || '{"sort": "promoted"}'::JSONB);
END
$$;

-- condenser_api / tags_api get_discussions_by_promoted (Steem-era method, restored)
CREATE OR REPLACE FUNCTION pixamind.condenser_get_discussions_by_promoted(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _limit INT;
  _truncate_body INT;
  _observer_id INT;
  _tag TEXT;
  _post_id INT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"start_author": "string", "start_permlink": "string", "limit": "number", "tag": "string", "truncate_body": "number", "filter_tags": "array", "observer": "string"}',
    2, '{"start_permlink": "permlink must be string", "start_author": "invalid account name type"}');
  IF hivemind_postgrest_utilities.parse_argument_from_json(_params, 'filter_tags', False) IS NOT NULL THEN
    RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_parameter_validation_exception('filter_tags not supported');
  END IF;
  _limit := hivemind_postgrest_utilities.valid_number(hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False),
                                                      least(20, hivemind_postgrest_utilities.get_max_posts_per_call_limit()),
                                                      1, hivemind_postgrest_utilities.get_max_posts_per_call_limit(), 'limit');
  _truncate_body := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'truncate_body', False), 0, NULL, NULL, 'truncate_body');
  _observer_id := hivemind_postgrest_utilities.find_account_id(
    hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'observer', False), True), True);
  _tag := hivemind_postgrest_utilities.valid_tag(
    hivemind_postgrest_utilities.valid_tag(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'tag', False), True), True);
  _post_id := hivemind_postgrest_utilities.find_comment_id(
    hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start_author', False), True),
    hivemind_postgrest_utilities.valid_permlink(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start_permlink', False), True),
    True);
  RETURN pixamind.get_promoted_ranked_posts(_post_id, _observer_id, _limit, _truncate_body, _tag, False);
END
$$;

CREATE OR REPLACE FUNCTION pixamind.api_get_promotions(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _author TEXT;
  _permlink TEXT;
  _sender TEXT;
  _limit INT;
  _post_id INT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"author": "string", "permlink": "string", "sender": "string", "limit": "number"}', 1, NULL);
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 100, 1, 1000, 'limit');
  _sender := hivemind_postgrest_utilities.parse_argument_from_json(_params, 'sender', False);

  IF _sender IS NOT NULL THEN
    _sender := hivemind_postgrest_utilities.valid_account(_sender);
    RETURN jsonb_build_object(
      'sender', _sender,
      'total', pixamind.stable_amount_text((SELECT sum(amount) FROM pixamind.post_promotions WHERE sender = _sender)),
      'promotions', COALESCE((
        SELECT jsonb_agg(jsonb_build_object('author', p.author, 'permlink', p.permlink,
                                            'amount', pixamind.stable_amount_text(p.amount), 'block_num', p.block_num,
                                            'timestamp', to_char(p.created_at, 'YYYY-MM-DD"T"HH24:MI:SS'),
                                            'post_exists', p.post_id IS NOT NULL) ORDER BY p.op_id DESC)
        FROM (SELECT * FROM pixamind.post_promotions WHERE sender = _sender ORDER BY op_id DESC LIMIT _limit) p
      ), '[]'::JSONB));
  END IF;

  _author := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'author', True));
  _permlink := hivemind_postgrest_utilities.valid_permlink(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'permlink', True));
  _post_id := hivemind_postgrest_utilities.find_comment_id(_author, _permlink, True);
  RETURN jsonb_build_object(
    'author', _author,
    'permlink', _permlink,
    'promoted', pixamind.stable_amount_text((SELECT promoted FROM pixamind.promoted_posts WHERE post_id = _post_id)),
    'promotions', COALESCE((
      SELECT jsonb_agg(jsonb_build_object('sender', p.sender, 'amount', pixamind.stable_amount_text(p.amount),
                                          'block_num', p.block_num,
                                          'timestamp', to_char(p.created_at, 'YYYY-MM-DD"T"HH24:MI:SS')) ORDER BY p.op_id DESC)
      FROM (SELECT * FROM pixamind.post_promotions WHERE post_id = _post_id ORDER BY op_id DESC LIMIT _limit) p
    ), '[]'::JSONB));
END
$$;

-- --------------------------------------------------------------- dispatch
CREATE OR REPLACE FUNCTION pixamind.dispatch(_api_type TEXT, _method_type TEXT, _params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
BEGIN
  CASE _api_type || '.' || _method_type
    WHEN 'pixamind_api.get_info' THEN RETURN pixamind.api_get_info(_params);
    WHEN 'pixamind_api.get_metrics' THEN RETURN pixamind.api_get_metrics(_params);
    WHEN 'pixamind_api.get_events' THEN RETURN pixamind.api_get_events(_params);
    WHEN 'pixamind_content.get_content_metadata' THEN RETURN pixamind.api_get_content_metadata(_params);
    WHEN 'pixamind_graph.get_mutual_follows' THEN RETURN pixamind.api_get_mutual_follows(_params);
    WHEN 'pixamind_graph.get_relationships' THEN RETURN pixamind.api_get_relationships(_params);
    WHEN 'pixamind_feed.get_promoted_posts' THEN RETURN pixamind.api_get_promoted_posts(_params);
    WHEN 'pixamind_feed.get_promotions' THEN RETURN pixamind.api_get_promotions(_params);
    ELSE
      RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_method_not_found_exception(_api_type || '.' || _method_type);
  END CASE;
END
$$;
