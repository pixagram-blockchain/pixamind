-- Pixamind runtime: projection fingerprints (determinism and integrity checks).
--
-- A fingerprint summarises a projection (a derived table) as a row count plus an
-- order-independent hash of its rows. Rows are rendered with natural keys
-- (account names, author/permlink) instead of surrogate ids, so two databases
-- built from the same blocks — two nodes, a replay, Hivemind vs Pixamind —
-- produce identical fingerprints even when sequences handed out different ids.
--
-- Values maintained by periodic jobs or by wall-clock time (account rank,
-- community pending/rank statistics, notification scores) are left out: they
-- differ between correct databases. Compare databases stopped at the same block
-- (`pixamind integrity --compare-url ...`, see docs/pixamind/TESTING.md).

CREATE OR REPLACE FUNCTION pixamind.fingerprint_projections()
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS
$$
  SELECT ARRAY['accounts', 'posts', 'post_data', 'post_tags', 'votes', 'follows', 'muted', 'blacklisted',
               'follow_muted', 'follow_blacklisted', 'reblogs', 'feed_cache', 'communities', 'roles',
               'subscriptions', 'mentions', 'notifications', 'events', 'promotions', 'promoted_posts']
$$;

-- SQL text of a projection: one TEXT column "r" per row.
CREATE OR REPLACE FUNCTION pixamind.fingerprint_sql(_projection TEXT)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
  -- post natural key: author/permlink#counter_deleted
  _pk CONSTANT TEXT := 'SELECT hp.id, ha.name || ''/'' || hpd.permlink || ''#'' || hp.counter_deleted AS k
                        FROM hivemind_app.hive_posts hp
                        JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
                        JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id';
  _follow_like CONSTANT TEXT := 'SELECT concat_ws(''|'', a1.name, a2.name, t.block_num) AS r
                                 FROM hivemind_app.%I t
                                 JOIN hivemind_app.hive_accounts a1 ON a1.id = t.follower
                                 JOIN hivemind_app.hive_accounts a2 ON a2.id = t.following';
BEGIN
  RETURN CASE _projection
    WHEN 'accounts' THEN
      'SELECT concat_ws(''|'', a.name, a.created_at, a.followers, a.following, a.lastread_at,
                        md5(COALESCE(a.posting_json_metadata, '''')), md5(COALESCE(a.json_metadata, ''''))) AS r
       FROM hivemind_app.hive_accounts a'
    WHEN 'posts' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', k.k, par.k, root.k, cd.category, hc.name, p.created_at, p.depth, p.is_pinned, p.is_muted,
                        p.muted_reasons, p.is_valid, p.children, p.payout, p.pending_payout, p.payout_at,
                        p.last_payout_at, p.updated_at, p.is_paidout, p.is_nsfw, p.is_declined, p.is_full_power,
                        p.is_hidden, p.sc_trend, p.sc_hot, p.total_payout_value, p.author_rewards,
                        p.author_rewards_hive, p.author_rewards_hbd, p.author_rewards_vests, p.abs_rshares,
                        p.vote_rshares, p.total_vote_weight, p.total_votes, p.net_votes, p.active, p.cashout_time,
                        p.percent_hbd, p.curator_payout_value, p.max_accepted_payout, p.allow_votes,
                        p.allow_curation_rewards, p.beneficiaries::TEXT, p.block_num, p.block_num_created,
                        p.last_payout_block) AS r
       FROM hivemind_app.hive_posts p
       JOIN pk k ON k.id = p.id
       LEFT JOIN pk par ON par.id = p.parent_id
       LEFT JOIN pk root ON root.id = p.root_id
       LEFT JOIN hivemind_app.hive_category_data cd ON cd.id = p.category_id
       LEFT JOIN hivemind_app.hive_communities hc ON hc.id = p.community_id
       WHERE p.id <> 0'
    WHEN 'post_data' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', k.k, md5(d.title), md5(d.body), md5(d.json)) AS r
       FROM hivemind_app.hive_post_data d JOIN pk k ON k.id = d.id'
    WHEN 'post_tags' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', k.k, td.tag) AS r
       FROM hivemind_app.hive_post_tags t JOIN pk k ON k.id = t.post_id
       JOIN hivemind_app.hive_tag_data td ON td.id = t.tag_id'
    WHEN 'votes' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', va.name, k.k, v.weight, v.rshares, v.vote_percent, v.last_update, v.num_changes,
                        v.block_num, v.is_effective) AS r
       FROM hivemind_app.hive_votes v JOIN pk k ON k.id = v.post_id
       JOIN hivemind_app.hive_accounts va ON va.id = v.voter_id'
    WHEN 'follows' THEN format(_follow_like, 'follows')
    WHEN 'muted' THEN format(_follow_like, 'muted')
    WHEN 'blacklisted' THEN format(_follow_like, 'blacklisted')
    WHEN 'follow_muted' THEN format(_follow_like, 'follow_muted')
    WHEN 'follow_blacklisted' THEN format(_follow_like, 'follow_blacklisted')
    WHEN 'reblogs' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', a.name, k.k, r.created_at, r.block_num) AS r
       FROM hivemind_app.hive_reblogs r JOIN pk k ON k.id = r.post_id
       JOIN hivemind_app.hive_accounts a ON a.id = r.blogger_id'
    WHEN 'feed_cache' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', a.name, k.k, f.created_at, f.block_num) AS r
       FROM hivemind_app.hive_feed_cache f JOIN pk k ON k.id = f.post_id
       JOIN hivemind_app.hive_accounts a ON a.id = f.account_id'
    WHEN 'communities' THEN
      'SELECT concat_ws(''|'', c.name, c.type_id, c.lang, c.title, c.created_at, c.subscribers, c.is_nsfw, c.about,
                        c.primary_tag, c.category, c.description, c.flag_text, c.settings::TEXT, c.block_num) AS r
       FROM hivemind_app.hive_communities c'
    WHEN 'roles' THEN
      'SELECT concat_ws(''|'', a.name, c.name, r.role_id, r.title, r.created_at) AS r
       FROM hivemind_app.hive_roles r
       JOIN hivemind_app.hive_accounts a ON a.id = r.account_id
       JOIN hivemind_app.hive_communities c ON c.id = r.community_id'
    WHEN 'subscriptions' THEN
      'SELECT concat_ws(''|'', a.name, c.name, s.created_at, s.block_num) AS r
       FROM hivemind_app.hive_subscriptions s
       JOIN hivemind_app.hive_accounts a ON a.id = s.account_id
       JOIN hivemind_app.hive_communities c ON c.id = s.community_id'
    WHEN 'mentions' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', k.k, a.name, m.block_num) AS r
       FROM hivemind_app.hive_mentions m JOIN pk k ON k.id = m.post_id
       JOIN hivemind_app.hive_accounts a ON a.id = m.account_id'
    WHEN 'notifications' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', n.type_id, src.name, dst.name, k.k, dk.k, n.block_num, n.created_at, n.community,
                        n.community_title, n.payload) AS r
       FROM hivemind_app.hive_notification_cache n
       LEFT JOIN hivemind_app.hive_accounts src ON src.id = n.src
       LEFT JOIN hivemind_app.hive_accounts dst ON dst.id = n.dst
       LEFT JOIN pk k ON k.id = n.post_id
       LEFT JOIN pk dk ON dk.id = n.dst_post_id'
    WHEN 'events' THEN
      'SELECT concat_ws(''|'', e.op_id, e.seq, e.block_num, e.created_at, e.type, e.account, e.target, e.payload::TEXT,
                        e.version) AS r
       FROM pixamind.events e'
    WHEN 'promotions' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', p.op_id, p.block_num, p.created_at, p.sender, p.author, p.permlink, k.k, p.amount) AS r
       FROM pixamind.post_promotions p LEFT JOIN pk k ON k.id = p.post_id'
    WHEN 'promoted_posts' THEN
      'WITH pk AS (' || _pk || ')
       SELECT concat_ws(''|'', k.k, p.promoted, p.last_promoted_at, p.last_block) AS r
       FROM pixamind.promoted_posts p JOIN pk k ON k.id = p.post_id'
  END;
END
$$;

-- 64-bit hash of a row rendering; summing these gives an order-independent digest.
CREATE OR REPLACE FUNCTION pixamind.row_hash(_r TEXT)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT ('x' || substr(md5(COALESCE(_r, '')), 1, 16))::BIT(64)::BIGINT::NUMERIC $$;

-- Bucket of a row rendering (first 16 bits of its md5), for drilling into differences.
CREATE OR REPLACE FUNCTION pixamind.row_bucket(_r TEXT, _buckets INT)
RETURNS INT LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT (('x' || substr(md5(COALESCE(_r, '')), 1, 4))::BIT(16)::INT) % _buckets $$;

CREATE OR REPLACE FUNCTION pixamind.projection_fingerprints(_projections TEXT[] DEFAULT NULL)
RETURNS TABLE (projection TEXT, rows BIGINT, fingerprint TEXT) LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _sum NUMERIC;
BEGIN
  FOREACH projection IN ARRAY COALESCE(_projections, pixamind.fingerprint_projections()) LOOP
    IF pixamind.fingerprint_sql(projection) IS NULL THEN
      RAISE EXCEPTION 'unknown projection: %', projection;
    END IF;
    EXECUTE format('SELECT count(*), COALESCE(sum(pixamind.row_hash(q.r)), 0) FROM (%s) q',
                   pixamind.fingerprint_sql(projection))
      INTO rows, _sum;
    fingerprint := left(md5(rows::TEXT || ':' || _sum::TEXT), 16);
    RETURN NEXT;
  END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION pixamind.projection_bucket_fingerprints(_projection TEXT, _buckets INT DEFAULT 256)
RETURNS TABLE (bucket INT, rows BIGINT, fingerprint TEXT) LANGUAGE plpgsql STABLE AS
$$
BEGIN
  IF _buckets < 1 OR _buckets > 65536 THEN
    RAISE EXCEPTION 'buckets must be between 1 and 65536';
  END IF;
  RETURN QUERY EXECUTE format(
    'SELECT pixamind.row_bucket(q.r, %s) AS bucket, count(*)::BIGINT,
            left(md5(count(*)::TEXT || '':'' || sum(pixamind.row_hash(q.r))::TEXT), 16)
     FROM (%s) q GROUP BY 1 ORDER BY 1', _buckets, pixamind.fingerprint_sql(_projection));
END
$$;

CREATE OR REPLACE FUNCTION pixamind.projection_rows(_projection TEXT, _bucket INT, _buckets INT DEFAULT 256, _limit INT DEFAULT 1000)
RETURNS SETOF TEXT LANGUAGE plpgsql STABLE AS
$$
BEGIN
  RETURN QUERY EXECUTE format(
    'SELECT q.r FROM (%s) q WHERE pixamind.row_bucket(q.r, %s) = %s ORDER BY q.r LIMIT %s',
    pixamind.fingerprint_sql(_projection), _buckets, _bucket, _limit);
END
$$;
