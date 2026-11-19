-- Pixamind runtime: the social event outbox (events contract v1).
--
-- Events are normalised social operations, derived from the operations hivemind
-- loads for each batch (hivemind_app._ops_staging), after hivemind has applied
-- them. They describe what happened on chain, checked the same way hivemind
-- checks it (signer, payload shape), not hivemind's resulting row changes.
--
--   vote                      {voter, author, permlink, weight}
--   post_created/post_updated {author, permlink, category, title, content}
--   comment_created/comment_updated {author, permlink, parent_author, parent_permlink, title, content}
--   post_deleted              {author, permlink}
--   follow                    {follower, following, action}     action: follow, unfollow, mute, blacklist, ...
--   reblog / reblog_deleted   {account, author, permlink}
--   community                 {action, community, ...}          as sent; roles are checked by hivemind
--   account_created           {account, creator}
--   account_updated           {account, metadata, posting_metadata}
--   author_reward             {author, permlink, hbd_payout, hive_payout, vesting_payout}
--   promotion                 {sender, author, permlink, amount}  (written by 50_promotions.sql)
--
-- Every batch first deletes its own block range, then rewrites it: replaying a
-- batch is harmless, and after a micro-fork the re-processed blocks replace the
-- abandoned ones. Readers are only shown blocks the context has processed.

CREATE OR REPLACE FUNCTION pixamind._emit_events_sql(_source TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$fn$
SELECT format($q$
WITH src AS (
    SELECT s.id, s.block_num, s.op_type_id, s.val
    FROM %1$s s
    WHERE s.block_num BETWEEN $1 AND $2
),
blk AS (
    SELECT num, created_at FROM hivemind_app.blocks_view WHERE num BETWEEN $1 AND $2
),
-- ------------------------------------------------------------------- votes
votes AS (
    SELECT s.id AS op_id, 0 AS seq, s.block_num, 'vote' AS type,
           s.val->>'voter' AS account, s.val->>'author' AS target,
           jsonb_build_object('voter', s.val->>'voter', 'author', s.val->>'author',
                              'permlink', s.val->>'permlink', 'weight', (s.val->>'weight')::INT) AS payload
    FROM src s
    WHERE s.op_type_id = 0
),
-- ------------------------------------------------------- posts and comments
comment_ops AS (
    SELECT s.id, s.block_num, s.val,
           s.val->>'author' AS author, s.val->>'permlink' AS permlink,
           COALESCE(s.val->>'parent_author', '') AS parent_author, s.val->>'parent_permlink' AS parent_permlink,
           row_number() OVER (PARTITION BY s.val->>'author', s.val->>'permlink', s.block_num ORDER BY s.id) AS nth_in_block
    FROM src s
    WHERE s.op_type_id = 1
),
comments AS (
    SELECT c.id AS op_id, 0 AS seq, c.block_num,
           CASE WHEN c.parent_author = '' THEN 'post' ELSE 'comment' END
           || CASE WHEN p.created_block = c.block_num AND c.nth_in_block = 1 THEN '_created' ELSE '_updated' END AS type,
           c.author AS account, NULLIF(c.parent_author, '') AS target,
           jsonb_strip_nulls(jsonb_build_object(
               'author', c.author,
               'permlink', c.permlink,
               'category', CASE WHEN c.parent_author = '' THEN c.parent_permlink END,
               'parent_author', NULLIF(c.parent_author, ''),
               'parent_permlink', CASE WHEN c.parent_author <> '' THEN c.parent_permlink END,
               'title', NULLIF(left(c.val->>'title', 256), ''),
               'content', NULLIF(pixamind.content_metadata(c.val->>'json_metadata', left(c.val->>'body', 160),
                                                           length(c.val->>'body')), '{}'::JSONB)
           )) AS payload
    FROM comment_ops c
    -- the latest creation of this author/permlink at or before the operation;
    -- none means hivemind rejected the operation, so no event either
    JOIN LATERAL (
        SELECT max(hp.block_num_created) AS created_block
        FROM hivemind_app.hive_posts hp
        WHERE hp.author_id = (SELECT ha.id FROM hivemind_app.hive_accounts ha WHERE ha.name = c.author)
          AND hp.permlink_id = (SELECT hpd.id FROM hivemind_app.hive_permlink_data hpd WHERE hpd.permlink = c.permlink)
          AND hp.block_num_created <= c.block_num
    ) p ON p.created_block IS NOT NULL
),
deletes AS (
    SELECT s.id AS op_id, 0 AS seq, s.block_num, 'post_deleted' AS type,
           s.val->>'author' AS account, NULL::TEXT AS target,
           jsonb_build_object('author', s.val->>'author', 'permlink', s.val->>'permlink') AS payload
    FROM src s
    WHERE s.op_type_id = 17
      AND NOT EXISTS (   -- ineffective_delete_comment: the chain refused the delete
          SELECT 1 FROM src x
          WHERE x.op_type_id = 73 AND x.block_num = s.block_num
            AND x.val->>'author' = s.val->>'author' AND x.val->>'permlink' = s.val->>'permlink')
),
-- ------------------------------------------------------------- custom_json
cj AS (
    SELECT s.id, s.block_num, s.val->>'id' AS cj_id,
           s.val->'required_posting_auths'->>0 AS signer,
           pixamind.try_jsonb(s.val->>'json') AS j
    FROM src s
    WHERE s.op_type_id = 18
      AND s.val->>'id' IN ('follow', 'reblog', 'community')
      AND jsonb_array_length(COALESCE(s.val->'required_auths', '[]'::JSONB)) = 0
      AND jsonb_array_length(COALESCE(s.val->'required_posting_auths', '[]'::JSONB)) = 1
),
cj_ops AS (   -- [name, {body}]; bare objects were accepted before block 6M (Steem legacy)
    SELECT id, block_num, cj_id, signer,
           CASE WHEN jsonb_typeof(j) = 'array' AND jsonb_array_length(j) = 2 THEN j->>0
                WHEN jsonb_typeof(j) = 'object' AND block_num < 6000000 AND cj_id = 'follow' AND j ? 'follower' THEN 'follow'
                WHEN jsonb_typeof(j) = 'object' AND block_num < 6000000 AND j ? 'author' THEN 'reblog'
           END AS name,
           CASE WHEN jsonb_typeof(j) = 'array' AND jsonb_array_length(j) = 2 THEN j->1
                WHEN jsonb_typeof(j) = 'object' AND block_num < 6000000 THEN j
           END AS body
    FROM cj
),
follow_ops AS (
    SELECT o.id, o.block_num, o.signer, o.body,
           CASE WHEN (o.body->'what'->>0) IN ('true', 'false') THEN '' ELSE COALESCE(o.body->'what'->>0, '') END AS what
    FROM cj_ops o
    WHERE o.cj_id = 'follow' AND o.name = 'follow'
      AND jsonb_typeof(o.body) = 'object' AND jsonb_typeof(o.body->'what') = 'array'
      AND o.body ? 'following'
      AND o.body->>'follower' = o.signer
),
follows AS (
    SELECT f.id AS op_id, (t.ord - 1)::INT AS seq, f.block_num, 'follow' AS type,
           f.signer AS account, t.following AS target,
           jsonb_build_object('follower', f.signer, 'following', t.following,
                              'action', CASE f.what WHEN 'blog' THEN 'follow' WHEN 'ignore' THEN 'mute'
                                                    WHEN '' THEN 'unfollow' ELSE f.what END) AS payload
    FROM follow_ops f
    CROSS JOIN LATERAL (
        SELECT x.following, x.ord FROM (
            SELECT CASE WHEN jsonb_typeof(f.body->'following') = 'array' THEN e.value #>> '{}'
                        ELSE f.body->>'following' END AS following,
                   e.ord
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(f.body->'following') = 'array'
                                           THEN f.body->'following' ELSE '[null]'::JSONB END)
                 WITH ORDINALITY AS e(value, ord)
        ) x
        WHERE x.following IS NOT NULL AND x.following <> '' AND x.following <> f.signer
    ) t
    WHERE f.what IN ('', 'blog', 'follow', 'ignore', 'blacklist', 'unblacklist', 'follow_blacklist',
                     'unfollow_blacklist', 'follow_muted', 'unfollow_muted')
    UNION ALL
    SELECT f.id, 0, f.block_num, 'follow', f.signer, NULL,
           jsonb_build_object('follower', f.signer, 'action', f.what)
    FROM follow_ops f
    WHERE f.what IN ('reset_blacklist', 'reset_following_list', 'reset_muted_list',
                     'reset_follow_blacklist', 'reset_follow_muted_list', 'reset_all_lists')
),
reblogs AS (
    SELECT o.id AS op_id, 0 AS seq, o.block_num,
           CASE WHEN o.body->>'delete' = 'delete' THEN 'reblog_deleted' ELSE 'reblog' END AS type,
           o.signer AS account, o.body->>'author' AS target,
           jsonb_build_object('account', o.signer, 'author', o.body->>'author', 'permlink', o.body->>'permlink') AS payload
    FROM cj_ops o
    WHERE o.cj_id IN ('follow', 'reblog') AND o.name = 'reblog'
      AND jsonb_typeof(o.body) = 'object'
      AND o.body->>'account' = o.signer
      AND COALESCE(o.body->>'author', '') <> '' AND COALESCE(o.body->>'permlink', '') <> ''
),
communities AS (
    SELECT o.id AS op_id, 0 AS seq, o.block_num, 'community' AS type,
           o.signer AS account, NULLIF(o.body->>'account', '') AS target,
           -- the operation's parameters, with action/community set last so a
           -- parameter of the same name cannot override them
           (o.body - 'action' - 'community')
             || jsonb_build_object('action', o.name, 'community', o.body->>'community') AS payload
    FROM cj_ops o
    WHERE o.cj_id = 'community'
      AND jsonb_typeof(o.body) = 'object'
      AND o.name IN ('subscribe', 'unsubscribe', 'setRole', 'setUserTitle', 'mutePost', 'unmutePost',
                     'pinPost', 'unpinPost', 'flagPost', 'updateProps')
      AND COALESCE(o.body->>'community', '') <> ''
      AND octet_length(o.body::TEXT) <= 8192
),
-- ---------------------------------------------------------------- accounts
accounts AS (
    SELECT s.id AS op_id, 0 AS seq, s.block_num, 'account_created' AS type,
           CASE s.op_type_id WHEN 14 THEN s.val->>'worker_account'
                             WHEN 30 THEN s.val->'work'->'value'->'input'->>'worker_account'
                             ELSE s.val->>'new_account_name' END AS account,
           s.val->>'creator' AS target,
           jsonb_strip_nulls(jsonb_build_object(
               'account', CASE s.op_type_id WHEN 14 THEN s.val->>'worker_account'
                                            WHEN 30 THEN s.val->'work'->'value'->'input'->>'worker_account'
                                            ELSE s.val->>'new_account_name' END,
               'creator', s.val->>'creator')) AS payload
    FROM src s
    WHERE s.op_type_id IN (9, 14, 23, 30, 41)
    UNION ALL
    SELECT s.id, 0, s.block_num, 'account_updated', s.val->>'account', NULL,
           jsonb_build_object('account', s.val->>'account',
                              'metadata', COALESCE(s.val->>'json_metadata', '') <> '',
                              'posting_metadata', COALESCE(s.val->>'posting_json_metadata', '') <> '')
    FROM src s
    WHERE s.op_type_id IN (10, 43)
      AND (COALESCE(s.val->>'json_metadata', '') <> '' OR COALESCE(s.val->>'posting_json_metadata', '') <> '')
),
rewards AS (
    SELECT s.id AS op_id, 0 AS seq, s.block_num, 'author_reward' AS type,
           s.val->>'author' AS account, NULL::TEXT AS target,
           jsonb_strip_nulls(jsonb_build_object(
               'author', s.val->>'author', 'permlink', s.val->>'permlink',
               'hbd_payout', pixamind.nai_text(s.val->'hbd_payout'),
               'hive_payout', pixamind.nai_text(s.val->'hive_payout'),
               'vesting_payout', pixamind.nai_text(s.val->'vesting_payout'))) AS payload
    FROM src s
    WHERE s.op_type_id = 51
),
all_events AS (
    SELECT * FROM votes
    UNION ALL SELECT * FROM comments
    UNION ALL SELECT * FROM deletes
    UNION ALL SELECT * FROM follows
    UNION ALL SELECT * FROM reblogs
    UNION ALL SELECT * FROM communities
    UNION ALL SELECT * FROM accounts
    UNION ALL SELECT * FROM rewards
)
INSERT INTO pixamind.events (op_id, seq, block_num, created_at, type, account, target, payload)
SELECT e.op_id, e.seq, e.block_num, b.created_at, e.type,
       pixamind.account_or_null(e.account), pixamind.account_or_null(e.target), e.payload
FROM all_events e
JOIN blk b ON b.num = e.block_num
WHERE e.seq BETWEEN 0 AND 32767
ON CONFLICT (op_id, seq) DO NOTHING
$q$, _source)
$fn$;

-- Write the events of blocks [_first, _last] (only blocks >= _min_block).
-- _source: 'staging' during sync (hivemind_app._ops_staging, already loaded for
-- the batch) or 'backfill' (pixamind._backfill_ops, see load_backfill_ops).
-- Returns the number of events written.
CREATE OR REPLACE FUNCTION pixamind.emit_events(_first INT, _last INT, _min_block INT DEFAULT 0, _source TEXT DEFAULT 'staging')
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _from INT := GREATEST(_first, COALESCE(_min_block, 0));
  _count INT;
BEGIN
  IF _last < _from THEN
    RETURN 0;
  END IF;
  DELETE FROM pixamind.events WHERE block_num BETWEEN _from AND _last AND type <> 'promotion';
  EXECUTE pixamind._emit_events_sql(
    CASE _source WHEN 'staging' THEN 'hivemind_app._ops_staging'
                 WHEN 'backfill' THEN 'pixamind._backfill_ops'
                 ELSE NULL END
  ) USING _from, _last;
  GET DIAGNOSTICS _count = ROW_COUNT;
  RETURN _count;
END
$$;

-- Load the operations of a block range from HAF for a backfill (same filter as
-- hivemind_app.load_ops_staging).
CREATE OR REPLACE FUNCTION pixamind.load_backfill_ops(_first INT, _last INT)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _count INT;
BEGIN
  TRUNCATE pixamind._backfill_ops;
  INSERT INTO pixamind._backfill_ops (id, block_num, block_date, op_type_id, val)
  SELECT ho.id, ho.block_num, b.created_at, ho.op_type_id, ho.body_value
  FROM hivemind_app.operations_view ho
  JOIN hivemind_app.blocks_view b ON b.num = ho.block_num
  WHERE ho.block_num BETWEEN _first AND _last
    AND ho.op_type_id IN (0, 1, 9, 10, 14, 17, 18, 19, 23, 30, 41, 43, 51, 53, 61, 72, 73)
    AND (ho.op_type_id <> 18 OR ho.custom_json_type_id IN (
        SELECT cjt.id FROM hafd.custom_json_types cjt
        WHERE cjt.custom_json_id IN ('follow', 'reblog', 'community', 'notify')));
  GET DIAGNOSTICS _count = ROW_COUNT;
  RETURN _count;
END
$$;

-- Drop events of blocks older than _keep_blocks behind the processed head.
CREATE OR REPLACE FUNCTION pixamind.prune_events(_keep_blocks INT)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _count INT;
  _head INT := hive.app_get_current_block_num('hivemind_app');
BEGIN
  IF _keep_blocks IS NULL OR _keep_blocks <= 0 THEN
    RETURN 0;
  END IF;
  DELETE FROM pixamind.events WHERE block_num < _head - _keep_blocks;
  GET DIAGNOSTICS _count = ROW_COUNT;
  RETURN _count;
END
$$;
