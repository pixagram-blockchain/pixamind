-- Pixamind runtime: native NFTs (Pixa chain hardfork 32).
--
-- On chain a post is minted, listed, unlisted, bought and transferred with one operation,
-- nft_operation (id 37), whose `action` says which. A purchase also produces the virtual operation
-- nft_sold_operation (id 93), which names the seller and the royalty; the sale is read from it.
--
--   nft_operation       {account, action: {type: nft_mint|nft_list|nft_unlist|nft_buy|nft_transfer, value: {...}}}
--   nft_sold_operation  {seller, buyer, author, permlink, edition, price, royalty}
--
-- An NFT has 1 to 10,000 copies (editions) numbered from 1. The API names the NFT "@author/permlink/0"
-- and its copy N "@author/permlink/N". Prices, royalties and the mint fee are in {{HBD_SYMBOL}}. A copy
-- transferred to the null account is burned. The license is the full text of the terms (up to 72 KiB);
-- the objects carry its length and SHA-256, and get_license returns the text.
--
-- A copy that was never offered nor moved has no row in pixamind.nft_editions, as it has no object on
-- chain: it is its creator's, acquired at mint, not offered. Every reader below answers for it all the
-- same (pixamind.nft_untouched_edition), so a client never sees the difference.
--
-- Those copies are the reserve. The creator can offer the whole reserve at one price: an nft_list of
-- copy 0, withdrawn by an nft_unlist of copy 0, recorded as list and unlist events of edition 0. A
-- buyer of copy 0 gets the lowest-numbered copy of the reserve; the nft_sold_operation of that sale
-- names it, so the sale is recorded against that copy like any other. When the creator withdraws the
-- offer of a copy that never left him, the chain removes the copy's state and the copy is back in
-- the reserve: its row goes too. The offer of the reserve ends by itself once no copy is left in it.
--
-- Every operation in a block was accepted by consensus, so nothing is re-checked here: the
-- projection records what happened (pixamind.nft_events) and folds it into the current state
-- (pixamind.nfts, pixamind.nft_editions).
--
--   pixamind_nft.get_nfts       up to 100 NFTs or copies by id: minted or not, terms, owners, offers
--   pixamind_nft.list_editions  the copies of one NFT, by number or cheapest first, optionally one owner's
--   pixamind_nft.list_created   NFTs an account minted
--   pixamind_nft.list_owned     NFTs an account holds copies of, with how many
--   pixamind_nft.list_market    copies and reserves offered for sale: newest first, or cheapest first
--   pixamind_nft.get_history    what happened to an NFT or to one copy, or what an account did with NFTs
--   pixamind_nft.get_license    the full license text of an NFT
--
-- Every post of the bridge, condenser and database APIs also carries `nft_minted`: the three post
-- builders of the inherited API call pixamind.nft_minted.
--
-- An offer can expire. Like the chain, the projection keeps the expired offer and every reader
-- compares it with the head block time of the indexed data, never with the wall clock.

-- The chain's "never" (time_point_sec::maximum()), the default expiration of an offer.
CREATE OR REPLACE FUNCTION pixamind.nft_never()
RETURNS TIMESTAMP LANGUAGE sql IMMUTABLE AS $$ SELECT TIMESTAMP '2106-02-07 06:28:15' $$;

-- "250.000 PXS"-style text of a price, a royalty, a fee or a total; NULL without an amount.
CREATE OR REPLACE FUNCTION pixamind.nft_amount_text(_amount NUMERIC)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$ SELECT to_char(_amount, 'FM999999999999999990.000') || ' {{HBD_SYMBOL}}' WHERE _amount IS NOT NULL $$;

CREATE OR REPLACE FUNCTION pixamind.nft_time_text(_time TIMESTAMP)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$ SELECT to_char(_time, 'YYYY-MM-DD"T"HH24:MI:SS') $$;

-- "@author/permlink/N": 0 names the NFT, 1 and up one of its copies.
CREATE OR REPLACE FUNCTION pixamind.nft_id(_author TEXT, _permlink TEXT, _edition INT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS
$$ SELECT '@' || _author || '/' || _permlink || '/' || _edition $$;

-- Reads an NFT id. The author runs to the first '/' and the number follows the last one, so a
-- permlink holding a '/' (the chain accepts any UTF-8) still reads correctly.
CREATE OR REPLACE FUNCTION pixamind.nft_parse_id(_id TEXT, OUT author TEXT, OUT permlink TEXT, OUT edition INT)
LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
  _parts TEXT[] := regexp_match(_id, '^@([^/]+)/(.+)/(0|[1-9][0-9]{0,4})$');
BEGIN
  IF _parts IS NULL OR _parts[3]::INT > 65535 THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception(
      'invalid NFT id ' || COALESCE(quote_literal(left(_id, 300)), 'null') || ', expected @author/permlink/N');
  END IF;
  author := hivemind_postgrest_utilities.valid_account(_parts[1]);
  permlink := hivemind_postgrest_utilities.valid_permlink(_parts[2]);
  edition := _parts[3]::INT;
END
$$;

-- Whether a post is minted. Every post object of the inherited post APIs carries it as `nft_minted`.
CREATE OR REPLACE FUNCTION pixamind.nft_minted(_author TEXT, _permlink TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS
$$ SELECT EXISTS (SELECT 1 FROM pixamind.nfts n WHERE n.author = _author AND n.permlink = _permlink) $$;

-- A copy that has no row: its creator's since the mint, never offered, never sold.
CREATE OR REPLACE FUNCTION pixamind.nft_untouched_edition(_n pixamind.nfts, _edition INT)
RETURNS pixamind.nft_editions LANGUAGE sql STABLE AS
$$
  SELECT jsonb_populate_record(NULL::pixamind.nft_editions, jsonb_build_object(
    'author', _n.author, 'permlink', _n.permlink, 'edition', _edition, 'owner', _n.author,
    'acquired_op_id', _n.mint_op_id, 'acquired_at', _n.minted_at, 'sales', 0, 'volume', 0, 'royalties', 0))
$$;

-- The offer of the reserve of an NFT, as the chain holds it after the events indexed so far: the last
-- list of copy 0, unless an unlist of copy 0 came after it or the reserve ran out after it (the chain
-- ends the offer when the last copy leaves the reserve, and does not bring it back when a copy returns).
-- The number of copies with state is followed event by event: a copy has state once it changed owner,
-- and before that only while an offer of its creator stands. All NULL when nothing is offered.
CREATE OR REPLACE FUNCTION pixamind.nft_reserve_offer_state(_author TEXT, _permlink TEXT,
  OUT price NUMERIC, OUT expiration TIMESTAMP, OUT op_id BIGINT, OUT created_at TIMESTAMP)
LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _offer pixamind.nft_events;
  _editions INT;
BEGIN
  SELECT e.* INTO _offer
  FROM pixamind.nft_events e
  WHERE e.author = _author AND e.permlink = _permlink AND e.edition = 0 AND e.type IN ('list', 'unlist')
  ORDER BY e.op_id DESC
  LIMIT 1;
  IF _offer.op_id IS NULL OR _offer.type <> 'list' THEN
    RETURN;
  END IF;
  SELECT n.editions INTO _editions FROM pixamind.nfts n WHERE n.author = _author AND n.permlink = _permlink;

  IF EXISTS (
    WITH states AS (   -- after each event of a copy: has the copy state on chain?
      SELECT e.op_id, e.edition,
             bool_or(e.type IN ('sale', 'transfer', 'burn')) OVER w OR e.type = 'list' AS has_state
      FROM pixamind.nft_events e
      WHERE e.author = _author AND e.permlink = _permlink AND e.edition > 0
      WINDOW w AS (PARTITION BY e.edition ORDER BY e.op_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    ),
    deltas AS (
      SELECT s.op_id,
             s.has_state::INT - COALESCE(lag(s.has_state::INT) OVER (PARTITION BY s.edition ORDER BY s.op_id), 0) AS delta
      FROM states s
    ),
    totals AS (        -- copies with state after each event of the NFT
      SELECT d.op_id, sum(d.delta) OVER (ORDER BY d.op_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS with_state
      FROM deltas d
    )
    SELECT 1 FROM totals t WHERE t.op_id > _offer.op_id AND t.with_state >= _editions
  ) THEN
    RETURN;            -- the reserve ran out after the offer: the chain ended it
  END IF;

  price := _offer.price;
  expiration := _offer.expiration;
  op_id := _offer.op_id;
  created_at := _offer.created_at;
END
$$;

-- Index the NFT operations of blocks [_first, _last] and rebuild what they touch: the row of an NFT
-- minted in the range, its reserve, and the row of each copy something happened to. Returns the
-- number of NFT events found.
CREATE OR REPLACE FUNCTION pixamind.process_nfts(_first INT, _last INT)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _count INT;
  _authors TEXT[];      -- what the range touches, as three parallel arrays: the NFT (author, permlink)
  _permlinks TEXT[];    -- and the copy, edition 0 standing for the NFT itself (its mint)
  _editions INT[];
  _new_authors TEXT[];
  _new_permlinks TEXT[];
  _new_editions INT[];
BEGIN
  -- replaying a range (crash recovery, micro-fork) replaces what it held
  WITH removed AS (
    DELETE FROM pixamind.nft_events WHERE block_num BETWEEN _first AND _last RETURNING author, permlink, edition
  )
  SELECT array_agg(d.author), array_agg(d.permlink), array_agg(d.edition) INTO _authors, _permlinks, _editions
  FROM (SELECT DISTINCT author, permlink, edition FROM removed) d;

  WITH ops AS (
    SELECT ho.id, ho.block_num, ho.op_type_id, ho.body_value AS v
    FROM hivemind_app.operations_view ho
    WHERE ho.block_num BETWEEN _first AND _last
      AND ho.op_type_id IN (37, 93)                          -- nft_operation, nft_sold_operation
  ),
  actions AS (
    SELECT o.id, o.block_num, o.v->>'account' AS account,
           o.v->'action'->>'type' AS kind, o.v->'action'->'value' AS a
    FROM ops o
    WHERE o.op_type_id = 37
  ),
  found AS (
    SELECT a.id, a.block_num, 'mint' AS type, a.account AS author, a.a->>'permlink' AS permlink, 0 AS edition,
           a.account, NULL::TEXT AS counterparty, NULL::NUMERIC AS price, NULL::NUMERIC AS royalty,
           pixamind.nai_value(a.a->'fee') AS fee, NULL::TIMESTAMP AS expiration,
           (a.a->>'editions')::INT AS editions, (a.a->>'royalty')::INT AS royalty_bps, a.a->>'license' AS license
    FROM actions a WHERE a.kind = 'nft_mint'
    UNION ALL
    SELECT a.id, a.block_num, 'list', a.a->>'author', a.a->>'permlink', (a.a->>'edition')::INT,
           a.account, NULL, pixamind.nai_value(a.a->'price'), NULL, NULL,
           (a.a->>'expiration')::TIMESTAMP, NULL, NULL, NULL
    FROM actions a WHERE a.kind = 'nft_list'
    UNION ALL
    SELECT a.id, a.block_num, 'unlist', a.a->>'author', a.a->>'permlink', (a.a->>'edition')::INT,
           a.account, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM actions a WHERE a.kind = 'nft_unlist'
    UNION ALL
    -- a gift; given to the null account, the copy is burned
    SELECT a.id, a.block_num, CASE WHEN a.a->>'to' = 'null' THEN 'burn' ELSE 'transfer' END,
           a.a->>'author', a.a->>'permlink', (a.a->>'edition')::INT,
           a.account, a.a->>'to', NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM actions a WHERE a.kind = 'nft_transfer'
    UNION ALL
    -- nft_buy itself is not recorded: its nft_sold_operation says the same and adds seller and royalty
    SELECT o.id, o.block_num, 'sale', o.v->>'author', o.v->>'permlink', (o.v->>'edition')::INT,
           o.v->>'buyer', o.v->>'seller', pixamind.nai_value(o.v->'price'), pixamind.nai_value(o.v->'royalty'),
           NULL, NULL, NULL, NULL, NULL
    FROM ops o WHERE o.op_type_id = 93
  ),
  inserted AS (
    INSERT INTO pixamind.nft_events (op_id, block_num, created_at, type, author, permlink, edition, account,
                                     counterparty, price, royalty, fee, expiration, editions, royalty_bps,
                                     license, license_sha256)
    SELECT f.id, f.block_num, b.created_at, f.type, f.author, f.permlink, f.edition, f.account,
           f.counterparty, f.price, f.royalty, f.fee, f.expiration, f.editions, f.royalty_bps,
           CASE WHEN f.type = 'mint' THEN COALESCE(f.license, '') END,
           -- the same hash the chain keeps: SHA-256 of the UTF-8 bytes of the text
           CASE WHEN f.type = 'mint' THEN encode(sha256(convert_to(COALESCE(f.license, ''), 'UTF8')), 'hex') END
    FROM found f
    JOIN hivemind_app.blocks_view b ON b.num = f.block_num
    ON CONFLICT (op_id) DO NOTHING
    RETURNING author, permlink, edition
  )
  SELECT count(*), array_agg(author::TEXT), array_agg(permlink::TEXT), array_agg(edition)
  INTO _count, _new_authors, _new_permlinks, _new_editions FROM inserted;

  SELECT array_agg(u.author), array_agg(u.permlink), array_agg(u.edition) INTO _authors, _permlinks, _editions
  FROM (SELECT r.author, r.permlink, r.edition FROM unnest(_authors, _permlinks, _editions) AS r(author, permlink, edition)
        UNION
        SELECT i.author, i.permlink, i.edition FROM unnest(_new_authors, _new_permlinks, _new_editions) AS i(author, permlink, edition)) u;

  IF _authors IS NULL THEN
    RETURN _count;      -- the usual case: no NFT operation in these blocks
  END IF;

  -- The NFTs minted in the range, or whose mint a fork took back: their row comes from the mint.
  DELETE FROM pixamind.nfts n USING unnest(_authors, _permlinks, _editions) AS t(author, permlink, edition)
  WHERE t.edition = 0 AND n.author = t.author AND n.permlink = t.permlink;

  INSERT INTO pixamind.nfts (author, permlink, post_id, mint_op_id, mint_block, minted_at, editions, royalty_bps,
                             license_size, license_sha256, mint_fee)
  SELECT m.author, m.permlink,
         (SELECT hp.id
            FROM hivemind_app.hive_posts hp
            JOIN hivemind_app.hive_accounts ha ON ha.id = hp.author_id
            JOIN hivemind_app.hive_permlink_data hpd ON hpd.id = hp.permlink_id
           WHERE ha.name = m.author AND hpd.permlink = m.permlink AND hp.counter_deleted = 0),
         m.op_id, m.block_num, m.created_at, m.editions, m.royalty_bps,
         octet_length(m.license), m.license_sha256, COALESCE(m.fee, 0)
  FROM pixamind.nft_events m
  JOIN unnest(_authors, _permlinks, _editions) AS t(author, permlink, edition)
    ON t.edition = 0 AND t.author = m.author AND t.permlink = m.permlink
  WHERE m.type = 'mint';

  -- The copies touched in the range, each rebuilt from its own whole history, which is short: the
  -- result is the same whatever the batch boundaries were and however often a range was replayed.
  -- A copy whose every event was taken back gets no row again: it is its creator's, untouched. So
  -- does a copy that never changed owner and whose last event withdrew its offer: it is back in the
  -- reserve, as on chain.
  DELETE FROM pixamind.nft_editions e USING unnest(_authors, _permlinks, _editions) AS t(author, permlink, edition)
  WHERE t.edition > 0 AND e.author = t.author AND e.permlink = t.permlink AND e.edition = t.edition;

  WITH touched AS (
    SELECT DISTINCT t.author, t.permlink, t.edition
    FROM unnest(_authors, _permlinks, _editions) AS t(author, permlink, edition)
    WHERE t.edition > 0
  ),
  history AS (
    SELECT e.*
    FROM pixamind.nft_events e
    JOIN touched t ON t.author = e.author AND t.permlink = e.permlink AND t.edition = e.edition
  ),
  copies AS (
    SELECT DISTINCT h.author, h.permlink, h.edition FROM history h
  ),
  owners AS (     -- the last change of owner; a copy without one is still with its creator
    SELECT DISTINCT ON (h.author, h.permlink, h.edition) h.author, h.permlink, h.edition, h.op_id, h.created_at,
           CASE h.type WHEN 'sale' THEN h.account ELSE h.counterparty END AS owner
    FROM history h
    WHERE h.type IN ('sale', 'transfer', 'burn')
    ORDER BY h.author, h.permlink, h.edition, h.op_id DESC
  ),
  offers AS (     -- the last event of the copy; only a 'list' leaves an offer standing
    SELECT DISTINCT ON (h.author, h.permlink, h.edition) h.author, h.permlink, h.edition, h.op_id, h.created_at,
           h.type, h.price, h.expiration
    FROM history h
    ORDER BY h.author, h.permlink, h.edition, h.op_id DESC
  ),
  sales AS (
    SELECT h.author, h.permlink, h.edition, count(*)::INT AS sales, sum(h.price) AS volume,
           sum(COALESCE(h.royalty, 0)) AS royalties
    FROM history h
    WHERE h.type = 'sale'
    GROUP BY h.author, h.permlink, h.edition
  ),
  last_sales AS (
    SELECT DISTINCT ON (h.author, h.permlink, h.edition) h.author, h.permlink, h.edition, h.op_id, h.price, h.created_at
    FROM history h
    WHERE h.type = 'sale'
    ORDER BY h.author, h.permlink, h.edition, h.op_id DESC
  )
  INSERT INTO pixamind.nft_editions (author, permlink, edition, owner, acquired_op_id, acquired_at, price, expiration,
                                     listed_op_id, listed_at, sales, volume, royalties,
                                     last_sale_op_id, last_sale_price, last_sale_at)
  SELECT n.author, n.permlink, c.edition,
         COALESCE(o.owner, n.author), COALESCE(o.op_id, n.mint_op_id), COALESCE(o.created_at, n.minted_at),
         CASE WHEN f.type = 'list' THEN f.price END,
         CASE WHEN f.type = 'list' THEN f.expiration END,
         CASE WHEN f.type = 'list' THEN f.op_id END,
         CASE WHEN f.type = 'list' THEN f.created_at END,
         COALESCE(s.sales, 0), COALESCE(s.volume, 0), COALESCE(s.royalties, 0),
         l.op_id, l.price, l.created_at
  FROM copies c
  JOIN pixamind.nfts n ON n.author = c.author AND n.permlink = c.permlink
  LEFT JOIN owners o ON o.author = c.author AND o.permlink = c.permlink AND o.edition = c.edition
  LEFT JOIN offers f ON f.author = c.author AND f.permlink = c.permlink AND f.edition = c.edition
  LEFT JOIN sales s ON s.author = c.author AND s.permlink = c.permlink AND s.edition = c.edition
  LEFT JOIN last_sales l ON l.author = c.author AND l.permlink = c.permlink AND l.edition = c.edition
  WHERE NOT (o.owner IS NULL AND f.type = 'unlist');

  -- The reserve of every NFT the range touched: what is left without a row, and the offer of copy 0.
  UPDATE pixamind.nfts n
  SET reserve = n.editions - (SELECT count(*) FROM pixamind.nft_editions e WHERE e.author = n.author AND e.permlink = n.permlink),
      reserve_price = r.price,
      reserve_expiration = r.expiration,
      reserve_listed_op_id = r.op_id,
      reserve_listed_at = r.created_at
  FROM (SELECT DISTINCT t.author, t.permlink FROM unnest(_authors, _permlinks) AS t(author, permlink)) t
  CROSS JOIN LATERAL pixamind.nft_reserve_offer_state(t.author, t.permlink) r
  WHERE n.author = t.author AND n.permlink = t.permlink;

  RETURN _count;
END
$$;

-- NFT events for blocks [_first, _last] (only when the event outbox is on). Every payload starts
-- with `nft_id`: "@author/permlink/0" for a mint, "@author/permlink/N" for anything done to copy N.
--   nft_minted       {nft_id, author, permlink, editions, royalty, license_size, license_sha256, fee}
--   nft_listed       {nft_id, account, author, permlink, edition, price, expiration}
--   nft_unlisted     {nft_id, account, author, permlink, edition}
--   nft_sold         {nft_id, buyer, seller, author, permlink, edition, price, royalty}
--   nft_transferred  {nft_id, from, to, author, permlink, edition}
--   nft_burned       {nft_id, account, author, permlink, edition}
-- `account` is who acted; `target` is who is affected: the creator of a listed, unlisted or burned
-- copy, the seller of a sold one, the receiver of a transferred one. nft_listed and nft_unlisted of
-- "@author/permlink/0" (edition 0) are the creator offering the reserve and withdrawing that offer; a
-- sale from the reserve is an nft_sold of the copy it gave. The license text is left out (it can be
-- 72 KiB): get_license returns it.
CREATE OR REPLACE FUNCTION pixamind.emit_nft_events(_first INT, _last INT, _min_block INT DEFAULT 0)
RETURNS INT LANGUAGE plpgsql VOLATILE AS
$$
DECLARE
  _from INT := GREATEST(_first, COALESCE(_min_block, 0));
  _count INT;
BEGIN
  IF _last < _from THEN
    RETURN 0;
  END IF;
  DELETE FROM pixamind.events
  WHERE block_num BETWEEN _from AND _last
    AND type IN ('nft_minted', 'nft_listed', 'nft_unlisted', 'nft_sold', 'nft_transferred', 'nft_burned');
  INSERT INTO pixamind.events (op_id, seq, block_num, created_at, type, account, target, payload)
  SELECT e.op_id, 0, e.block_num, e.created_at,
         CASE e.type WHEN 'mint' THEN 'nft_minted' WHEN 'list' THEN 'nft_listed' WHEN 'unlist' THEN 'nft_unlisted'
                     WHEN 'sale' THEN 'nft_sold' WHEN 'burn' THEN 'nft_burned' ELSE 'nft_transferred' END,
         e.account,
         CASE e.type WHEN 'mint' THEN NULL WHEN 'sale' THEN e.counterparty WHEN 'transfer' THEN e.counterparty
                     ELSE e.author END,
         jsonb_build_object('nft_id', pixamind.nft_id(e.author, e.permlink, e.edition)) ||
         CASE e.type
           WHEN 'mint' THEN jsonb_build_object('author', e.author, 'permlink', e.permlink, 'editions', e.editions,
                                               'royalty', e.royalty_bps, 'license_size', octet_length(e.license),
                                               'license_sha256', e.license_sha256,
                                               'fee', pixamind.nft_amount_text(COALESCE(e.fee, 0)))
           WHEN 'list' THEN jsonb_strip_nulls(jsonb_build_object(
                                               'account', e.account, 'author', e.author, 'permlink', e.permlink,
                                               'edition', e.edition, 'price', pixamind.nft_amount_text(e.price),
                                               'expiration', CASE WHEN e.expiration < pixamind.nft_never()
                                                                  THEN pixamind.nft_time_text(e.expiration) END))
           WHEN 'sale' THEN jsonb_build_object('buyer', e.account, 'seller', e.counterparty, 'author', e.author,
                                               'permlink', e.permlink, 'edition', e.edition,
                                               'price', pixamind.nft_amount_text(e.price),
                                               'royalty', pixamind.nft_amount_text(COALESCE(e.royalty, 0)))
           WHEN 'transfer' THEN jsonb_build_object('from', e.account, 'to', e.counterparty, 'author', e.author,
                                                   'permlink', e.permlink, 'edition', e.edition)
           ELSE jsonb_build_object('account', e.account, 'author', e.author, 'permlink', e.permlink,   -- unlist, burn
                                   'edition', e.edition)
         END
  FROM pixamind.nft_events e
  WHERE e.block_num BETWEEN _from AND _last
  ON CONFLICT (op_id, seq) DO NOTHING;
  GET DIAGNOSTICS _count = ROW_COUNT;
  RETURN _count;
END
$$;

-- ---------------------------------------------------------------- objects
-- One copy as the API shows it. `listed` is true only while the offer can be taken; a value that
-- does not apply is left out.
CREATE OR REPLACE FUNCTION pixamind.nft_edition_object(_e pixamind.nft_editions, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql IMMUTABLE AS
$$
  SELECT jsonb_strip_nulls(jsonb_build_object(
    'nft_id', pixamind.nft_id(_e.author, _e.permlink, _e.edition),
    'author', _e.author,
    'permlink', _e.permlink,
    'edition', _e.edition,
    'owner', _e.owner,
    'burned', _e.owner = 'null',
    'acquired', pixamind.nft_time_text(_e.acquired_at),
    'listed', COALESCE(_e.price IS NOT NULL AND _e.expiration > _now, FALSE),
    'price', CASE WHEN _e.price IS NOT NULL AND _e.expiration > _now THEN pixamind.nft_amount_text(_e.price) END,
    'expiration', CASE WHEN _e.price IS NOT NULL AND _e.expiration > _now AND _e.expiration < pixamind.nft_never()
                       THEN pixamind.nft_time_text(_e.expiration) END,
    'listed_at', CASE WHEN _e.price IS NOT NULL AND _e.expiration > _now THEN pixamind.nft_time_text(_e.listed_at) END,
    'sales', _e.sales,
    'volume', pixamind.nft_amount_text(_e.volume),
    'royalties', pixamind.nft_amount_text(_e.royalties),
    'last_sale_price', pixamind.nft_amount_text(_e.last_sale_price),
    'last_sale', pixamind.nft_time_text(_e.last_sale_at)))
$$;

-- The terms of an NFT, fixed at mint. The license is described by its length and SHA-256 (the
-- chain keeps the same); get_license returns the text.
CREATE OR REPLACE FUNCTION pixamind.nft_terms_object(_n pixamind.nfts)
RETURNS JSONB LANGUAGE sql IMMUTABLE AS
$$
  SELECT jsonb_build_object(
    'nft_id', pixamind.nft_id(_n.author, _n.permlink, 0),
    'author', _n.author,
    'permlink', _n.permlink,
    'minted', pixamind.nft_time_text(_n.minted_at),
    'mint_block', _n.mint_block,
    'editions', _n.editions,
    'royalty', _n.royalty_bps,
    'royalty_percent', to_char(_n.royalty_bps / 100.0, 'FM990.00'),
    'license_size', _n.license_size,
    'license_sha256', _n.license_sha256,
    'mint_fee', pixamind.nft_amount_text(_n.mint_fee),
    'post_exists', _n.post_id IS NOT NULL)
$$;

-- Whether copy 0 can be bought now: the reserve is offered, the offer has not expired and a copy is left.
CREATE OR REPLACE FUNCTION pixamind.nft_reserve_listed(_n pixamind.nfts, _now TIMESTAMP)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS
$$ SELECT COALESCE(_n.reserve > 0 AND _n.reserve_price IS NOT NULL AND _n.reserve_expiration > _now, FALSE) $$;

-- The copy a buyer of copy 0 gets: the lowest-numbered copy without a row. NULL when the reserve is empty.
CREATE OR REPLACE FUNCTION pixamind.nft_reserve_next(_n pixamind.nfts)
RETURNS INT LANGUAGE sql STABLE AS
$$
  SELECT CASE
    WHEN _n.reserve <= 0 THEN NULL
    WHEN NOT EXISTS (SELECT 1 FROM pixamind.nft_editions e
                     WHERE e.author = _n.author AND e.permlink = _n.permlink AND e.edition = 1) THEN 1
    ELSE (SELECT min(e.edition) + 1
          FROM pixamind.nft_editions e
          WHERE e.author = _n.author AND e.permlink = _n.permlink AND e.edition < _n.editions
            AND NOT EXISTS (SELECT 1 FROM pixamind.nft_editions x
                            WHERE x.author = e.author AND x.permlink = e.permlink AND x.edition = e.edition + 1))
  END
$$;

-- The offer of the reserve as the API shows it, shaped like a copy: "@author/permlink/0", how many
-- copies it covers and the copy the next buyer gets (`next`). NULL when copy 0 cannot be bought now.
CREATE OR REPLACE FUNCTION pixamind.nft_reserve_object(_n pixamind.nfts, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT jsonb_strip_nulls(jsonb_build_object(
    'nft_id', pixamind.nft_id(_n.author, _n.permlink, 0),
    'author', _n.author,
    'permlink', _n.permlink,
    'edition', 0,
    'owner', _n.author,
    'reserve', TRUE,
    'copies', _n.reserve,
    'next', pixamind.nft_id(_n.author, _n.permlink, pixamind.nft_reserve_next(_n)),
    'listed', TRUE,
    'price', pixamind.nft_amount_text(_n.reserve_price),
    'expiration', CASE WHEN _n.reserve_expiration < pixamind.nft_never() THEN pixamind.nft_time_text(_n.reserve_expiration) END,
    'listed_at', pixamind.nft_time_text(_n.reserve_listed_at)))
  WHERE pixamind.nft_reserve_listed(_n, _now)
$$;

-- The terms and the market summary of an NFT, from the rows of its copies (a copy without a row is
-- the creator's, in the reserve):
--   copies           the number of copies (editions)    burned     copies given to null
--   for_sale         copies that can be bought now: each copy offered on its own, plus the whole
--                    reserve while it is offered
--   listed           copies offered on their own        reserve    copies in the reserve
--   reserve_offer    the offer of the reserve, when it can be taken: price, copies, next
--   held_by_creator  copies the creator still has       sales      sales of all copies
--   floor            the lowest price a copy can be bought at now, as {price, copies, nft_ids}: how
--                    many copies sell at that price and the ids to buy them with, at most 100,
--                    "@author/permlink/0" first when the reserve sells at that price
--   last_sale        the most recent sale of any copy, as {price, edition, time}
--   volume           what all sales paid                royalties  what they paid the creator
CREATE OR REPLACE FUNCTION pixamind.nft_summary_object(_n pixamind.nfts, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  WITH copies AS (
    SELECT e.* FROM pixamind.nft_editions e WHERE e.author = _n.author AND e.permlink = _n.permlink
  ),
  offers AS (          -- what a buyer can take now; edition 0 is the reserve
    SELECT c.edition, c.price, 1 AS copies FROM copies c WHERE c.price IS NOT NULL AND c.expiration > _now
    UNION ALL
    SELECT 0, _n.reserve_price, _n.reserve WHERE pixamind.nft_reserve_listed(_n, _now)
  ),
  floor AS (
    SELECT min(o.price) AS price FROM offers o
  )
  SELECT pixamind.nft_terms_object(_n) || jsonb_build_object(
    'copies', _n.editions,
    'for_sale', (SELECT COALESCE(sum(o.copies), 0) FROM offers o),
    'listed', (SELECT count(*) FROM offers o WHERE o.edition > 0),
    'reserve', _n.reserve,
    'burned', (SELECT count(*) FROM copies c WHERE c.owner = 'null'),
    'held_by_creator', _n.editions - (SELECT count(*) FROM copies) + (SELECT count(*) FROM copies c WHERE c.owner = _n.author),
    'sales', (SELECT COALESCE(sum(c.sales), 0) FROM copies c),
    'volume', pixamind.nft_amount_text((SELECT COALESCE(sum(c.volume), 0) FROM copies c)),
    'royalties', pixamind.nft_amount_text((SELECT COALESCE(sum(c.royalties), 0) FROM copies c)))
  || CASE WHEN pixamind.nft_reserve_listed(_n, _now)
          THEN jsonb_build_object('reserve_offer', pixamind.nft_reserve_object(_n, _now)) ELSE '{}'::JSONB END
  || COALESCE((
       SELECT jsonb_build_object('floor', jsonb_build_object(
                'price', pixamind.nft_amount_text(f.price),
                'copies', (SELECT sum(o.copies) FROM offers o WHERE o.price = f.price),
                'nft_ids', (SELECT jsonb_agg(pixamind.nft_id(_n.author, _n.permlink, x.edition) ORDER BY x.edition)
                            FROM (SELECT o.edition FROM offers o WHERE o.price = f.price ORDER BY o.edition LIMIT 100) x)))
       FROM floor f
       WHERE f.price IS NOT NULL), '{}'::JSONB)
  || COALESCE((
       SELECT jsonb_build_object('last_sale', jsonb_build_object(
                'price', pixamind.nft_amount_text(c.last_sale_price),
                'edition', c.edition,
                'time', pixamind.nft_time_text(c.last_sale_at)))
       FROM copies c
       WHERE c.last_sale_op_id IS NOT NULL
       ORDER BY c.last_sale_op_id DESC
       LIMIT 1), '{}'::JSONB)
$$;

-- An NFT with its terms and market summary; NULL when the post is not minted. get_nfts ("/0"),
-- content metadata and list_created use it.
CREATE OR REPLACE FUNCTION pixamind.nft_object(_author TEXT, _permlink TEXT, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT pixamind.nft_summary_object(n, _now) FROM pixamind.nfts n WHERE n.author = _author AND n.permlink = _permlink
$$;

-- A copy as the API shows it, `reserve: true` when it has no row: it is in the creator's reserve,
-- bought through copy 0 while the reserve is offered, never on its own.
CREATE OR REPLACE FUNCTION pixamind.nft_any_edition_object(_n pixamind.nfts, _e pixamind.nft_editions, _edition INT, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT CASE WHEN _e.author IS NULL
              THEN pixamind.nft_edition_object(pixamind.nft_untouched_edition(_n, _edition), _now) || '{"reserve": true}'::JSONB
              ELSE pixamind.nft_edition_object(_e, _now) END
$$;

-- "@author/permlink/N": copy N with the terms of its NFT under `nft`, as list_market shows copies;
-- NULL when the post is not minted or has no copy N.
CREATE OR REPLACE FUNCTION pixamind.nft_copy_object(_author TEXT, _permlink TEXT, _edition INT, _now TIMESTAMP)
RETURNS JSONB LANGUAGE sql STABLE AS
$$
  SELECT pixamind.nft_any_edition_object(n,
           (SELECT e FROM pixamind.nft_editions e
             WHERE e.author = n.author AND e.permlink = n.permlink AND e.edition = _edition), _edition, _now)
         || jsonb_build_object('nft', pixamind.nft_terms_object(n))
  FROM pixamind.nfts n
  WHERE n.author = _author AND n.permlink = _permlink AND _edition BETWEEN 1 AND n.editions
$$;

-- Head block time of the indexed data: what "now" means for an offer's expiration.
CREATE OR REPLACE FUNCTION pixamind.nft_now()
RETURNS TIMESTAMP LANGUAGE sql STABLE AS
$$ SELECT COALESCE(hivemind_app.head_block_time(), TIMESTAMP '1970-01-01') $$;

-- Paging cursor of the list methods: the operation id as a string (op ids exceed 2^53).
CREATE OR REPLACE FUNCTION pixamind.nft_parse_cursor(_cursor TEXT)
RETURNS BIGINT LANGUAGE plpgsql IMMUTABLE AS
$$
BEGIN
  IF _cursor IS NULL OR _cursor = '' THEN
    RETURN NULL;
  END IF;
  IF _cursor ~ '^[0-9]{1,18}$' THEN
    RETURN _cursor::BIGINT;
  END IF;
  RAISE EXCEPTION '%', hivemind_postgrest_utilities.raise_parameter_validation_exception('invalid cursor: ' || left(_cursor, 64));
END
$$;

-- Price cursor "<price in thousandths>-<op id>" of the lists sorted by price.
CREATE OR REPLACE FUNCTION pixamind.nft_parse_price_cursor(_cursor TEXT, OUT price NUMERIC, OUT op_id BIGINT)
LANGUAGE plpgsql IMMUTABLE AS
$$
BEGIN
  IF _cursor IS NULL OR _cursor = '' THEN
    RETURN;
  END IF;
  IF _cursor !~ '^[0-9]{1,19}-[0-9]{1,18}$' THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('invalid cursor: ' || left(_cursor, 64));
  END IF;
  price := split_part(_cursor, '-', 1)::NUMERIC / 1000;
  op_id := split_part(_cursor, '-', 2)::BIGINT;
END
$$;

-- An optional account argument: NULL when absent or empty.
CREATE OR REPLACE FUNCTION pixamind.nft_optional_account(_params JSONB, _name TEXT)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
  _account TEXT := hivemind_postgrest_utilities.parse_argument_from_json(_params, _name, False);
BEGIN
  IF _account IS NULL OR _account = '' THEN
    RETURN NULL;
  END IF;
  RETURN hivemind_postgrest_utilities.valid_account(_account);
END
$$;

-- ------------------------------------------------------------- pixamind_nft
-- get_nfts {nfts: ["@author/permlink/N", ...]} (at most 100) | {nft: "@author/permlink/N"}
-- "/0" returns the NFT with its terms and market summary (its copies: list_editions); "/N" returns
-- copy N with the terms of its NFT. An id that names nothing is left out, so an empty answer means
-- "not minted". Each id is answered once, in the order first asked.
CREATE OR REPLACE FUNCTION pixamind.api_nft_get_nfts(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _now TIMESTAMP := pixamind.nft_now();
  _ids JSONB;
  _result JSONB;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params, '{"nft": "string", "nfts": "array"}', 1, NULL);
  IF _params ? 'nft' AND _params ? 'nfts' THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('give nft or nfts, not both');
  END IF;

  IF _params ? 'nfts' THEN
    _ids := _params->'nfts';
    IF jsonb_array_length(_ids) > 100 THEN
      PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('at most 100 NFT ids');
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(_ids) v WHERE jsonb_typeof(v) <> 'string') THEN
      PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('NFT ids are strings like @author/permlink/0');
    END IF;
  ELSE
    _ids := jsonb_build_array(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'nft', True));
  END IF;

  SELECT COALESCE(jsonb_agg(q.obj ORDER BY q.ord), '[]'::JSONB)
  INTO _result
  FROM (
    SELECT min(i.ord) AS ord,
           CASE WHEN p.edition = 0 THEN pixamind.nft_object(p.author, p.permlink, _now)
                ELSE pixamind.nft_copy_object(p.author, p.permlink, p.edition, _now) END AS obj
    FROM jsonb_array_elements_text(_ids) WITH ORDINALITY AS i(id, ord)
    CROSS JOIN LATERAL pixamind.nft_parse_id(i.id) p
    GROUP BY p.author, p.permlink, p.edition
  ) q
  WHERE q.obj IS NOT NULL;

  RETURN jsonb_build_object('nfts', _result);
END
$$;

-- list_editions {nft: "@author/permlink/N", sort = "number" | "price", owner, limit = 20 (max 100), start}
-- The copies of one NFT. "number": every copy in number order, each with its owner and offer, the copies
-- of the reserve included (`reserve: true`); "price": what can be bought now, cheapest first - each copy
-- offered on its own, and the offer of the reserve as one entry, copy 0 (`copies`, `next`). With `owner`,
-- only what that account holds. `start` is the next_start of the previous page. Any copy number in the
-- id names the NFT.
CREATE OR REPLACE FUNCTION pixamind.api_nft_list_editions(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _now TIMESTAMP := pixamind.nft_now();
  _id RECORD;
  _n pixamind.nfts;
  _sort TEXT;
  _owner TEXT;
  _limit INT;
  _start TEXT;
  _from INT := 1;
  _cursor RECORD;
  _editions JSONB;
  _count INT;
  _next TEXT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"nft": "string", "sort": "string", "owner": "string", "limit": "number", "start": "string"}', 1, NULL);
  SELECT p.author, p.permlink INTO _id
  FROM pixamind.nft_parse_id(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'nft', True)) p;
  _sort := COALESCE(NULLIF(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'sort', False), ''), 'number');
  IF _sort NOT IN ('number', 'price') THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('sort must be number or price');
  END IF;
  _owner := pixamind.nft_optional_account(_params, 'owner');
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 20, 1, 100, 'limit');
  _start := hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False);

  SELECT n.* INTO _n FROM pixamind.nfts n WHERE n.author = _id.author AND n.permlink = _id.permlink;

  IF _sort = 'number' THEN
    -- cursor: the number of the first copy of the page
    IF _start IS NOT NULL AND _start <> '' THEN
      IF _start !~ '^[1-9][0-9]{0,4}$' THEN
        PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('invalid cursor: ' || left(_start, 64));
      END IF;
      _from := _start::INT;
    END IF;
    IF _n.author IS NULL THEN
      _editions := '[]'::JSONB;
      _count := 0;
    ELSIF _owner IS NULL OR _owner = _n.author THEN
      -- every copy by number; one without a row is the creator's, untouched
      SELECT COALESCE(jsonb_agg(pixamind.nft_any_edition_object(_n, p.e, p.edition, _now) ORDER BY p.edition), '[]'::JSONB),
             count(*), (max(p.edition) + 1)::TEXT
      INTO _editions, _count, _next
      FROM (SELECT g.edition, e AS e
            FROM generate_series(_from, _n.editions) AS g(edition)
            LEFT JOIN pixamind.nft_editions e
              ON e.author = _n.author AND e.permlink = _n.permlink AND e.edition = g.edition
            WHERE _owner IS NULL OR COALESCE(e.owner, _n.author) = _owner
            ORDER BY g.edition
            LIMIT _limit) p;
    ELSE
      -- another account's copies all have a row: they left the creator at some point
      SELECT COALESCE(jsonb_agg(pixamind.nft_edition_object(p.e, _now) ORDER BY p.edition), '[]'::JSONB),
             count(*), (max(p.edition) + 1)::TEXT
      INTO _editions, _count, _next
      FROM (SELECT e AS e, e.edition
            FROM pixamind.nft_editions e
            WHERE e.author = _n.author AND e.permlink = _n.permlink AND e.owner = _owner AND e.edition >= _from
            ORDER BY e.edition
            LIMIT _limit) p;
    END IF;
  ELSE
    SELECT c.price, c.op_id INTO _cursor FROM pixamind.nft_parse_price_cursor(_start) c;
    SELECT COALESCE(jsonb_agg(CASE WHEN p.edition = 0 THEN pixamind.nft_reserve_object(_n, _now)
                                   ELSE pixamind.nft_edition_object(p.e, _now) END
                              ORDER BY p.price, p.listed_op_id), '[]'::JSONB),
           count(*),
           (array_agg((p.price * 1000)::BIGINT::TEXT || '-' || p.listed_op_id::TEXT ORDER BY p.price DESC, p.listed_op_id DESC))[1]
    INTO _editions, _count, _next
    FROM (SELECT o.*
          FROM (SELECT e AS e, e.edition, e.price, e.listed_op_id
                FROM pixamind.nft_editions e
                WHERE e.author = _id.author AND e.permlink = _id.permlink
                  AND e.price IS NOT NULL AND e.expiration > _now
                  AND (_owner IS NULL OR e.owner = _owner)
                UNION ALL
                SELECT NULL::pixamind.nft_editions, 0, _n.reserve_price, _n.reserve_listed_op_id
                WHERE pixamind.nft_reserve_listed(_n, _now) AND (_owner IS NULL OR _owner = _n.author)) o
          WHERE _cursor.op_id IS NULL OR (o.price, o.listed_op_id) > (_cursor.price, _cursor.op_id)
          ORDER BY o.price, o.listed_op_id
          LIMIT _limit) p;
  END IF;

  RETURN jsonb_strip_nulls(jsonb_build_object(
    'nft_id', pixamind.nft_id(_id.author, _id.permlink, 0),
    'sort', _sort,
    'owner', _owner,
    'editions', _editions,
    'next_start', CASE WHEN _count = _limit THEN _next END,
    'has_more', _count = _limit));
END
$$;

-- list_created {account, limit = 20 (max 100), start}  - NFTs the account minted, newest first,
-- each with its terms and market summary (the copies themselves: list_editions)
CREATE OR REPLACE FUNCTION pixamind.api_nft_list_created(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _now TIMESTAMP := pixamind.nft_now();
  _account TEXT;
  _limit INT;
  _start BIGINT;
  _nfts JSONB;
  _count INT;
  _next BIGINT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"account": "string", "limit": "number", "start": "string"}', 1, NULL);
  _account := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'account', True));
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 20, 1, 100, 'limit');
  _start := pixamind.nft_parse_cursor(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False));

  SELECT COALESCE(jsonb_agg(pixamind.nft_object(p.author, p.permlink, _now) ORDER BY p.mint_op_id DESC), '[]'::JSONB),
         count(*), min(p.mint_op_id)
  INTO _nfts, _count, _next
  FROM (SELECT n.author, n.permlink, n.mint_op_id
        FROM pixamind.nfts n
        WHERE n.author = _account AND (_start IS NULL OR n.mint_op_id < _start)
        ORDER BY n.mint_op_id DESC
        LIMIT _limit) p;

  RETURN jsonb_build_object(
    'account', _account,
    'total', (SELECT count(*) FROM pixamind.nfts n WHERE n.author = _account),
    'nfts', _nfts,
    'next_start', CASE WHEN _count = _limit THEN _next::TEXT END,
    'has_more', _count = _limit);
END
$$;

-- list_owned {account, acquired_only = false, limit = 20 (max 100), start}
-- The NFTs the account holds copies of, the one it most recently got a copy of first. Each entry has
-- the terms of the NFT, `held` (how many copies the account holds), `listed` (how many of those can be
-- bought now) and `acquired` (when it got the latest one). A creator holds every copy of its NFT that
-- it never parted with. With acquired_only, those are left out: what remains is what it bought or was
-- given. The copies themselves: list_editions with `owner`.
CREATE OR REPLACE FUNCTION pixamind.api_nft_list_owned(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _now TIMESTAMP := pixamind.nft_now();
  _account TEXT;
  _acquired_only BOOLEAN;
  _limit INT;
  _start BIGINT;
  _nfts JSONB;
  _count INT;
  _next BIGINT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"account": "string", "acquired_only": "boolean", "limit": "number", "start": "string"}', 1, NULL);
  _account := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'account', True));
  _acquired_only := COALESCE((_params->>'acquired_only')::BOOLEAN, FALSE);
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 20, 1, 100, 'limit');
  _start := pixamind.nft_parse_cursor(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False));

  -- The latest acquisition identifies the NFT: a mint or a sale, transfer or burn concerns one NFT only.
  WITH holdings AS (
    SELECT e.author, e.permlink, count(*) AS held,
           count(*) FILTER (WHERE e.price IS NOT NULL AND e.expiration > _now) AS listed,
           max(e.acquired_op_id) AS last_op, max(e.acquired_at) AS last_at
    FROM pixamind.nft_editions e
    JOIN pixamind.nfts n ON n.author = e.author AND n.permlink = e.permlink
    WHERE e.owner = _account
      AND (NOT _acquired_only OR e.acquired_op_id <> n.mint_op_id)
    GROUP BY e.author, e.permlink
    UNION ALL
    -- the copies of its own NFTs that have no row: never offered nor moved, still its own since the mint
    SELECT n.author, n.permlink,
           n.editions - (SELECT count(*) FROM pixamind.nft_editions x WHERE x.author = n.author AND x.permlink = n.permlink),
           0, n.mint_op_id, n.minted_at
    FROM pixamind.nfts n
    WHERE n.author = _account AND NOT _acquired_only
  ),
  held_nfts AS (
    SELECT h.author, h.permlink, sum(h.held)::INT AS held, sum(h.listed)::INT AS listed,
           max(h.last_op) AS last_op, max(h.last_at) AS last_at
    FROM holdings h
    GROUP BY h.author, h.permlink
    HAVING sum(h.held) > 0
  )
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'nft', pixamind.nft_terms_object(n),
                    'held', p.held,
                    'listed', p.listed,
                    'acquired', pixamind.nft_time_text(p.last_at)) ORDER BY p.last_op DESC), '[]'::JSONB),
         count(*), min(p.last_op)
  INTO _nfts, _count, _next
  FROM (SELECT h.*
        FROM held_nfts h
        WHERE _start IS NULL OR h.last_op < _start
        ORDER BY h.last_op DESC
        LIMIT _limit) p
  JOIN pixamind.nfts n ON n.author = p.author AND n.permlink = p.permlink;

  RETURN jsonb_build_object(
    'account', _account,
    'nfts', _nfts,
    'next_start', CASE WHEN _count = _limit THEN _next::TEXT END,
    'has_more', _count = _limit);
END
$$;

-- list_market {sort = "recent" | "price", creator, limit = 20 (max 100), start}
-- What can be bought now, each with the terms of its NFT: every copy offered on its own, and every
-- offered reserve as one entry, copy 0 (`reserve: true`, `copies`, `next`); newest offers first, or
-- cheapest first. `start` is the next_start of the previous page.
CREATE OR REPLACE FUNCTION pixamind.api_nft_list_market(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _now TIMESTAMP := pixamind.nft_now();
  _sort TEXT;
  _creator TEXT;
  _limit INT;
  _start TEXT;
  _start_op BIGINT;
  _cursor RECORD;
  _editions JSONB;
  _count INT;
  _next TEXT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"sort": "string", "creator": "string", "limit": "number", "start": "string"}', 0, NULL);
  _sort := COALESCE(NULLIF(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'sort', False), ''), 'recent');
  IF _sort NOT IN ('recent', 'price') THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('sort must be recent or price');
  END IF;
  _creator := pixamind.nft_optional_account(_params, 'creator');
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 20, 1, 100, 'limit');
  _start := hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False);

  IF _sort = 'price' THEN
    SELECT c.price, c.op_id INTO _cursor FROM pixamind.nft_parse_price_cursor(_start) c;
    SELECT COALESCE(jsonb_agg(CASE WHEN p.e IS NULL THEN pixamind.nft_reserve_object(p.n, _now)
                                   ELSE pixamind.nft_edition_object(p.e, _now) END
                              || jsonb_build_object('nft', pixamind.nft_terms_object(p.n))
                              ORDER BY p.price, p.listed_op_id), '[]'::JSONB),
           count(*),
           (array_agg((p.price * 1000)::BIGINT::TEXT || '-' || p.listed_op_id::TEXT ORDER BY p.price DESC, p.listed_op_id DESC))[1]
    INTO _editions, _count, _next
    FROM (SELECT o.*
          FROM (SELECT e AS e, n AS n, e.price, e.listed_op_id
                FROM pixamind.nft_editions e
                JOIN pixamind.nfts n ON n.author = e.author AND n.permlink = e.permlink
                WHERE e.price IS NOT NULL AND e.expiration > _now
                  AND (_creator IS NULL OR e.author = _creator)
                UNION ALL
                SELECT NULL::pixamind.nft_editions, n AS n, n.reserve_price, n.reserve_listed_op_id
                FROM pixamind.nfts n
                WHERE n.reserve_price IS NOT NULL AND pixamind.nft_reserve_listed(n, _now)
                  AND (_creator IS NULL OR n.author = _creator)) o
          WHERE _cursor.op_id IS NULL OR (o.price, o.listed_op_id) > (_cursor.price, _cursor.op_id)
          ORDER BY o.price, o.listed_op_id
          LIMIT _limit) p;
  ELSE
    _start_op := pixamind.nft_parse_cursor(_start);
    SELECT COALESCE(jsonb_agg(CASE WHEN p.e IS NULL THEN pixamind.nft_reserve_object(p.n, _now)
                                   ELSE pixamind.nft_edition_object(p.e, _now) END
                              || jsonb_build_object('nft', pixamind.nft_terms_object(p.n))
                              ORDER BY p.listed_op_id DESC), '[]'::JSONB),
           count(*), min(p.listed_op_id)::TEXT
    INTO _editions, _count, _next
    FROM (SELECT o.*
          FROM (SELECT e AS e, n AS n, e.listed_op_id
                FROM pixamind.nft_editions e
                JOIN pixamind.nfts n ON n.author = e.author AND n.permlink = e.permlink
                WHERE e.price IS NOT NULL AND e.expiration > _now
                  AND (_creator IS NULL OR e.author = _creator)
                UNION ALL
                SELECT NULL::pixamind.nft_editions, n AS n, n.reserve_listed_op_id
                FROM pixamind.nfts n
                WHERE n.reserve_price IS NOT NULL AND pixamind.nft_reserve_listed(n, _now)
                  AND (_creator IS NULL OR n.author = _creator)) o
          WHERE _start_op IS NULL OR o.listed_op_id < _start_op
          ORDER BY o.listed_op_id DESC
          LIMIT _limit) p;
  END IF;

  RETURN jsonb_build_object(
    'sort', _sort,
    'editions', _editions,
    'next_start', CASE WHEN _count = _limit THEN _next END,
    'has_more', _count = _limit);
END
$$;

-- get_history {nft: "@author/permlink/N"} | {account}   + {limit = 50 (max 1000), start}
-- Newest first. For "/0": the mint and everything that happened to every copy; for "/N": the mint
-- and what happened to copy N. For an account: every NFT action it took part in, as actor or
-- counterparty. Each entry has the `nft_id` it concerns.
CREATE OR REPLACE FUNCTION pixamind.api_nft_get_history(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _author TEXT;
  _permlink TEXT;
  _edition INT;
  _account TEXT;
  _limit INT;
  _start BIGINT;
  _events JSONB;
  _count INT;
  _next BIGINT;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params,
    '{"nft": "string", "account": "string", "limit": "number", "start": "string"}', 1, NULL);
  IF (_params ? 'nft') = (_params ? 'account') THEN
    PERFORM hivemind_postgrest_utilities.raise_parameter_validation_exception('give either nft or account');
  END IF;
  _limit := hivemind_postgrest_utilities.valid_number(
    hivemind_postgrest_utilities.parse_integer_argument_from_json(_params, 'limit', False), 50, 1, 1000, 'limit');
  _start := pixamind.nft_parse_cursor(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'start', False));

  IF _params ? 'account' THEN
    _account := hivemind_postgrest_utilities.valid_account(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'account', True));
  ELSE
    SELECT p.author, p.permlink, p.edition INTO _author, _permlink, _edition
    FROM pixamind.nft_parse_id(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'nft', True)) p;
  END IF;

  SELECT COALESCE(jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
           'id', p.op_id::TEXT,
           'block_num', p.block_num,
           'timestamp', pixamind.nft_time_text(p.created_at),
           'type', p.type,
           'nft_id', pixamind.nft_id(p.author, p.permlink, p.edition),
           'author', p.author,
           'permlink', p.permlink,
           'edition', NULLIF(p.edition, 0),
           'account', p.account,
           'seller', CASE WHEN p.type = 'sale' THEN p.counterparty END,
           'to', CASE WHEN p.type = 'transfer' THEN p.counterparty END,
           'price', pixamind.nft_amount_text(p.price),
           'royalty', CASE WHEN p.type = 'sale' THEN pixamind.nft_amount_text(COALESCE(p.royalty, 0)) END,
           'expiration', CASE WHEN p.expiration < pixamind.nft_never() THEN pixamind.nft_time_text(p.expiration) END,
           'editions', p.editions,
           'royalty_rate', p.royalty_bps,
           'license_size', octet_length(p.license),
           'license_sha256', p.license_sha256,
           'fee', CASE WHEN p.type = 'mint' THEN pixamind.nft_amount_text(COALESCE(p.fee, 0)) END)) ORDER BY p.op_id DESC), '[]'::JSONB),
         count(*), min(p.op_id)
  INTO _events, _count, _next
  FROM (SELECT e.*
        FROM pixamind.nft_events e
        WHERE (_start IS NULL OR e.op_id < _start)
          AND CASE
                WHEN _account IS NOT NULL THEN e.account = _account OR e.counterparty = _account
                ELSE e.author = _author AND e.permlink = _permlink
                     AND (_edition = 0 OR e.edition = _edition OR e.edition = 0)
              END
        ORDER BY e.op_id DESC
        LIMIT _limit) p;

  RETURN jsonb_build_object(
    'history', _events,
    'next_start', CASE WHEN _count = _limit THEN _next::TEXT END,
    'has_more', _count = _limit);
END
$$;

-- get_license {nft: "@author/permlink/N"}  - the full license text of the NFT, as its mint operation
-- carries it, with its length and SHA-256 (the hash the chain keeps); {} when the post is not minted.
-- Any copy number names the NFT.
CREATE OR REPLACE FUNCTION pixamind.api_nft_get_license(_params JSONB)
RETURNS JSONB LANGUAGE plpgsql STABLE AS
$$
DECLARE
  _id RECORD;
  _result JSONB;
BEGIN
  _params := hivemind_postgrest_utilities.validate_json_arguments(_params, '{"nft": "string"}', 1, NULL);
  SELECT p.author, p.permlink INTO _id
  FROM pixamind.nft_parse_id(hivemind_postgrest_utilities.parse_argument_from_json(_params, 'nft', True)) p;

  SELECT jsonb_build_object(
           'nft_id', pixamind.nft_id(n.author, n.permlink, 0),
           'license', m.license,
           'license_size', n.license_size,
           'license_sha256', n.license_sha256)
  INTO _result
  FROM pixamind.nfts n
  JOIN pixamind.nft_events m ON m.op_id = n.mint_op_id
  WHERE n.author = _id.author AND n.permlink = _id.permlink;

  RETURN COALESCE(_result, '{}'::JSONB);
END
$$;
