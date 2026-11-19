-- Pixamind runtime: paid post promotion (the "Promoted" feed).
--
-- A promotion is a transfer of the stable token (PXS on Pixa, NAI @@000000013)
-- to @null with the memo "@author/permlink". The chain burns what @null
-- receives, so promoting needs no consensus change: it is an indexer feature.
-- Steem's hivemind had it; Hive removed it from hivemind in January 2025;
-- Pixamind restores it with the same rules (stable token only, exact memo
-- format, unpaid posts only in the feed).

-- Index the promotions of blocks [_first, _last] and refresh the totals of the
-- posts they touch. Returns the number of promotion payments found.
CREATE OR REPLACE FUNCTION pixamind.process_promotions(_first INT, _last INT)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _affected INT[];
  _count INT;
BEGIN
  -- replaying a range (crash recovery, micro-fork) replaces what it held
  WITH removed AS (
    DELETE FROM pixamind.post_promotions WHERE block_num BETWEEN _first AND _last RETURNING post_id
  )
  SELECT array_agg(DISTINCT post_id) FILTER (WHERE post_id IS NOT NULL) INTO _affected FROM removed;

  WITH payments AS (
    SELECT ho.id, ho.block_num,
           ho.body_value->>'from' AS sender,
           btrim(ho.body_value->>'memo') AS memo,
           pixamind.nai_value(ho.body_value->'amount') AS amount
    FROM hivemind_app.operations_view ho
    WHERE ho.block_num BETWEEN _first AND _last
      AND ho.op_type_id = 2                                   -- transfer_operation
      AND ho.body_value->>'to' = 'null'
      AND ho.body_value->'amount'->>'nai' = '@@000000013'     -- stable token only
  ),
  parsed AS (
    SELECT p.*, split_part(substr(p.memo, 2), '/', 1) AS author, split_part(substr(p.memo, 2), '/', 2) AS permlink
    FROM payments p
    WHERE p.memo ~ '^@[a-z0-9.-]{3,16}/[^/\s]{1,255}$' AND p.amount > 0
  ),
  inserted AS (
    INSERT INTO pixamind.post_promotions (op_id, block_num, created_at, sender, author, permlink, post_id, amount)
    SELECT pa.id, pa.block_num, b.created_at, left(pa.sender, 16), pa.author, pa.permlink,
           (SELECT hp.id
              FROM hivemind_app.hive_posts hp
              JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
              JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
             WHERE ha.name = pa.author AND hpd.permlink = pa.permlink AND hp.counter_deleted = 0),
           pa.amount
    FROM parsed pa
    JOIN hivemind_app.blocks_view b ON b.num = pa.block_num
    ON CONFLICT (op_id) DO NOTHING
    RETURNING post_id
  )
  SELECT count(*), array_cat(_affected, array_agg(DISTINCT post_id) FILTER (WHERE post_id IS NOT NULL))
  INTO _count, _affected FROM inserted;

  IF _affected IS NOT NULL AND cardinality(_affected) > 0 THEN
    DELETE FROM pixamind.promoted_posts WHERE post_id = ANY(_affected);
    INSERT INTO pixamind.promoted_posts (post_id, promoted, last_promoted_at, last_block)
    SELECT pp.post_id, sum(pp.amount), max(pp.created_at), max(pp.block_num)
    FROM pixamind.post_promotions pp
    WHERE pp.post_id = ANY(_affected)
    GROUP BY pp.post_id;
  END IF;
  RETURN _count;
END
$$;

-- Promotion events for blocks [_first, _last] (only when the event outbox is on).
CREATE OR REPLACE FUNCTION pixamind.emit_promotion_events(_first INT, _last INT, _min_block INT DEFAULT 0)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _from INT := GREATEST(_first, COALESCE(_min_block, 0));
  _count INT;
BEGIN
  IF _last < _from THEN
    RETURN 0;
  END IF;
  DELETE FROM pixamind.events WHERE block_num BETWEEN _from AND _last AND type = 'promotion';
  INSERT INTO pixamind.events (op_id, seq, block_num, created_at, type, account, target, payload)
  SELECT pp.op_id, 0, pp.block_num, pp.created_at, 'promotion', pp.sender, pp.author,
         jsonb_build_object('sender', pp.sender, 'author', pp.author, 'permlink', pp.permlink,
                            'amount', pixamind.stable_amount_text(pp.amount),
                            'post_exists', pp.post_id IS NOT NULL)
  FROM pixamind.post_promotions pp
  WHERE pp.block_num BETWEEN _from AND _last
  ON CONFLICT (op_id, seq) DO NOTHING;
  GET DIAGNOSTICS _count = ROW_COUNT;
  RETURN _count;
END
$$;

-- Posts ranked by promotion, as bridge (or condenser) post objects with a
-- "promoted" amount. _tag: NULL/''/'all', a community name, or a tag.
-- Paging: start from the post after _post_id (0 = from the top).
CREATE OR REPLACE FUNCTION pixamind.get_promoted_ranked_posts(
    _post_id INT, _observer_id INT, _limit INT, _truncate_body INT, _tag TEXT,
    _called_from_bridge_api BOOLEAN, _muted_reasons_filter_mask INT DEFAULT NULL
)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _promoted_limit NUMERIC;
  _community_id INT;
  _tag_id INT;
  _result JSONB;
BEGIN
  IF _post_id <> 0 THEN
    SELECT promoted INTO _promoted_limit FROM pixamind.promoted_posts WHERE post_id = _post_id;
    IF _promoted_limit IS NULL THEN
      RETURN '[]'::JSONB;   -- the start post is not promoted: nothing ranks after it
    END IF;
  END IF;

  IF _tag IS NOT NULL AND _tag <> '' AND _tag <> 'all' THEN
    IF hivemind_postgrest_utilities.check_community(_tag) THEN
      _community_id := hivemind_postgrest_utilities.find_community_id(_tag, True);
    ELSE
      _tag_id := hivemind_postgrest_utilities.find_tag_id(_tag, True);
    END IF;
  END IF;

  _result = (
    SELECT jsonb_agg(
      (CASE
         WHEN _called_from_bridge_api
           THEN hivemind_postgrest_utilities.create_bridge_post_object(_observer_id, row, _truncate_body, NULL, row.is_pinned, True)
         ELSE hivemind_postgrest_utilities.create_condenser_post_object(_observer_id, row, _truncate_body, False)
       END) || jsonb_build_object('promoted', pixamind.stable_amount_text(row.promoted))
      ORDER BY row.promoted DESC, row.id DESC
    )
    FROM (
      WITH promoted AS (
        SELECT pp.post_id AS id, pp.promoted
        FROM pixamind.promoted_posts pp
        JOIN hivemind_app.live_posts_view hp ON hp.id = pp.post_id
        WHERE NOT hp.is_paidout
          AND pp.promoted > 0
          AND (_post_id = 0 OR pp.promoted < _promoted_limit OR (pp.promoted = _promoted_limit AND pp.post_id < _post_id))
          AND (_community_id IS NULL OR hp.community_id = _community_id)
          AND (_tag_id IS NULL OR EXISTS (SELECT 1 FROM hivemind_app.hive_post_tags hpt
                                          WHERE hpt.post_id = hp.id AND hpt.tag_id = _tag_id))
          AND (_observer_id = 0 OR NOT EXISTS (SELECT 1 FROM hivemind_app.muted_accounts_by_id_view
                                               WHERE observer_id = _observer_id AND muted_id = hp.author_id))
          AND (_muted_reasons_filter_mask IS NULL OR _muted_reasons_filter_mask = 0
               OR (hp.muted_reasons & _muted_reasons_filter_mask) = 0)
        ORDER BY pp.promoted DESC, pp.post_id DESC
        LIMIT _limit
      )
      SELECT
        hp.id, hp.author, hp.parent_author, hp.author_rep, hp.root_title, hp.beneficiaries,
        hp.max_accepted_payout, hp.percent_hbd, hp.url, hp.permlink, hp.parent_permlink_or_category,
        hp.title, hp.body, hp.category, hp.depth, hp.payout, hp.pending_payout, hp.payout_at,
        hp.is_paidout, hp.children, hp.votes, hp.created_at, hp.updated_at, hp.rshares,
        hp.abs_rshares, hp.json, hp.is_hidden, hp.is_grayed, hp.total_votes, hp.sc_trend,
        hp.role_title, hp.community_title, hp.role_id, hp.is_pinned, hp.curator_payout_value,
        hp.is_muted, hp.source AS blacklists, hp.muted_reasons,
        promoted.promoted
      FROM promoted,
      LATERAL hivemind_app.get_full_post_view_by_id(promoted.id, _observer_id) hp
    ) row
  );

  RETURN COALESCE(_result, '[]'::JSONB);
END
$$;
