-- Pixamind runtime: post and comment version history (table in
-- migrations/0003_post_versions.sql; docs/pixamind/POST-HISTORY.md).
--
-- Rows are written by the indexer (pixamind/indexer/history.py, right after
-- hivemind merges each comment operation) and by `pixamind history backfill`.
-- NULL columns mean "unchanged since the previous version" (or, in a creation
-- row of a post never edited, "as hivemind stores it now"); the functions below
-- give every version in full.

-- Every version of a post, oldest first, with full title/body/json_metadata.
-- changed: the fields that differ from the previous version (all three for the
-- creation; NULL for a first recorded version that is not the creation, whose
-- previous state is unknown).
CREATE OR REPLACE FUNCTION pixamind.post_version_states(_post_id INT)
RETURNS TABLE (op_id BIGINT, version INT, block_num INT, created_at TIMESTAMP, is_creation BOOLEAN,
               title TEXT, body TEXT, json_metadata TEXT, changed TEXT[])
LANGUAGE sql STABLE AS
$$
  WITH grouped AS (
    SELECT v.*,
           row_number() OVER w AS n,
           count(v.title) OVER w AS title_group,
           count(v.body) OVER w AS body_group,
           count(v.json_metadata) OVER w AS json_group
      FROM pixamind.post_versions v
     WHERE v.post_id = _post_id
    WINDOW w AS (ORDER BY v.op_id)
  ), filled AS (   -- group 0: nothing stored yet, the text is hivemind's current one
    SELECT g.op_id, g.n, g.block_num, g.created_at, g.is_creation,
           COALESCE(first_value(g.title) OVER (PARTITION BY g.title_group ORDER BY g.op_id), d.title) AS title,
           COALESCE(first_value(g.body) OVER (PARTITION BY g.body_group ORDER BY g.op_id), d.body) AS body,
           COALESCE(first_value(g.json_metadata) OVER (PARTITION BY g.json_group ORDER BY g.op_id), d.json) AS json_metadata
      FROM grouped g
      LEFT JOIN hivemind_app.hive_post_data d ON d.id = _post_id
  )
  SELECT f.op_id, f.n::INT, f.block_num, f.created_at, f.is_creation, f.title, f.body, f.json_metadata,
         CASE
           WHEN p.op_id IS NULL AND NOT f.is_creation THEN NULL
           ELSE array_remove(ARRAY[
                  CASE WHEN p.op_id IS NULL OR f.title IS DISTINCT FROM p.title THEN 'title' END,
                  CASE WHEN p.op_id IS NULL OR f.body IS DISTINCT FROM p.body THEN 'body' END,
                  CASE WHEN p.op_id IS NULL OR f.json_metadata IS DISTINCT FROM p.json_metadata THEN 'json_metadata' END],
                NULL)
         END
    FROM filled f
    LEFT JOIN filled p ON p.n = f.n - 1
   ORDER BY f.n
$$;

-- Insert versions given as a JSON array of
-- {op_id, post_id, block_num, created_at, is_creation, title, body, json_metadata}.
-- A version already recorded (same op_id) is kept, except that a creation row
-- still without text receives it (written again, complete, at the first edit).
-- Returns the rows inserted or filled.
CREATE OR REPLACE FUNCTION pixamind.write_post_versions(_rows JSONB)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _n INT;
BEGIN
  INSERT INTO pixamind.post_versions (op_id, post_id, block_num, created_at, is_creation, title, body, json_metadata)
  SELECT r.op_id, r.post_id, r.block_num, r.created_at, r.is_creation, r.title, r.body, r.json_metadata
    FROM jsonb_to_recordset(_rows) AS r(op_id BIGINT, post_id INT, block_num INT, created_at TIMESTAMP,
                                        is_creation BOOLEAN, title TEXT, body TEXT, json_metadata TEXT)
  ON CONFLICT (op_id) DO UPDATE
    SET title = EXCLUDED.title, body = EXCLUDED.body, json_metadata = EXCLUDED.json_metadata
    WHERE pixamind.post_versions.is_creation
      AND pixamind.post_versions.title IS NULL AND pixamind.post_versions.body IS NULL
      AND pixamind.post_versions.json_metadata IS NULL
      AND num_nonnulls(EXCLUDED.title, EXCLUDED.body, EXCLUDED.json_metadata) = 3;
  GET DIAGNOSTICS _n = ROW_COUNT;
  RETURN _n;
END
$$;

-- pixamind_content.get_post_history
--   {"author": "alice", "permlink": "cat", "start": 1, "limit": 20, "bodies": true}
CREATE OR REPLACE FUNCTION pixamind.api_get_post_history(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _author TEXT;
  _permlink TEXT;
  _post_id INT;
  _start INT;
  _limit INT;
  _bodies BOOLEAN;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"author": "string", "permlink": "string", "start": "number", "limit": "number", "bodies": "boolean"}', 2, NULL);
  _author := hivemind_postgrest_utilities.valid_account(
    hivemind_postgrest_utilities.parse_argument_from_json(_params, 'author', True));
  _permlink := hivemind_postgrest_utilities.valid_permlink(
    hivemind_postgrest_utilities.parse_argument_from_json(_params, 'permlink', True));
  _start := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'start', False), 1, 1, 2147483647, 'start');
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 20, 1, 100, 'limit');
  _bodies := COALESCE(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'bodies', False)::BOOLEAN, TRUE);
  _post_id := hivemind_postgrest_utilities.find_comment_id(_author, _permlink, True);

  RETURN (
    SELECT jsonb_build_object(
      'author', _author,
      'permlink', _permlink,
      'total', count(*),
      'complete', COALESCE(bool_or(s.is_creation), FALSE),
      'versions', COALESCE(jsonb_agg(
          jsonb_build_object(
            'version', s.version,
            'block_num', s.block_num,
            'timestamp', to_char(s.created_at, 'YYYY-MM-DD"T"HH24:MI:SS'),
            'op_id', s.op_id::TEXT,
            'created', s.is_creation,
            'changed', to_jsonb(s.changed),
            'title', s.title,
            'json_metadata', s.json_metadata)
          || CASE WHEN _bodies THEN jsonb_build_object('body', s.body) ELSE '{}'::JSONB END
          ORDER BY s.version) FILTER (WHERE s.version >= _start AND s.version < _start + _limit), '[]'::JSONB))
    FROM pixamind.post_version_states(_post_id) s);
END
$$;
