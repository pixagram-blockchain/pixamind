-- Pixamind 0002: the shared feeds cache.
--
-- A feed requested without an observer is the same for every user: the
-- Pixagram front page asks for the same 20 trending posts thousands of times
-- between two blocks. After each live block commits, the indexer computes every
-- target below once (pixamind.refresh_feed_cache), and hivemind_endpoints.home
-- answers a request with exactly the same api, method and params from here.
-- A row is served only while the hivemind_app context is still at the block it
-- was computed for, so the API never returns anything older than what it would
-- compute itself, and the bytes are the same. See docs/pixamind/FEED-CACHE.md.

-- The requests to keep warm. Add your own, e.g. a busy community's front page:
--   INSERT INTO pixamind.feed_cache_targets (api, method, params, note)
--   VALUES ('bridge', 'get_ranked_posts', '{"sort": "trending", "tag": "portal-100001", "limit": 20}', 'busy portal');
-- Matching is on the jsonb value (key order and spacing do not matter, number
-- spelling does: 20 is not 20.0), never on the request id. Only methods whose answer depends on indexed data alone belong
-- here; `hive` and `pixamind_api` report ages and clocks, so they are refused.
CREATE TABLE IF NOT EXISTS pixamind.feed_cache_targets (
    id      SERIAL PRIMARY KEY,
    api     TEXT NOT NULL CHECK (api NOT IN ('hive', 'pixamind_api')),
    method  TEXT NOT NULL,
    params  JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    note    TEXT,
    UNIQUE (api, method, params)
);

-- The cached results. UNLOGGED: rewritten after every block, so they are kept
-- out of the WAL (and off replicas, where the API simply computes); a crash
-- restart empties the table and the next block fills it again.
CREATE UNLOGGED TABLE IF NOT EXISTS pixamind.feed_cache (
    api           TEXT NOT NULL,
    method        TEXT NOT NULL,
    params        JSONB NOT NULL,
    block_num     INT NOT NULL,   -- served only while the context is at this block
    session_limit TEXT NOT NULL,  -- hivemind_app.max_posts_per_call it was computed with ('' = unset)
    result        JSON,           -- hivemind_postgrest_utilities.dispatch() output; NULL when it failed
    hash          TEXT,           -- md5 of result, to skip rewriting an unchanged feed
    changed_block INT NOT NULL,   -- block at which result last changed
    error         TEXT,           -- why result is NULL
    PRIMARY KEY (api, method, params)
) WITH (fillfactor = 50);

-- The observer-less first pages the Pixagram web app asks for
-- (pixagram-blockchain.github.io: src/js/pages/Feed.js, FeedPersonal.js,
-- src/js/utils/api/pixaproxyapi.js and service-worker.js).
INSERT INTO pixamind.feed_cache_targets (api, method, params, note) VALUES
    ('bridge', 'get_ranked_posts', '{"sort": "trending", "tag": "", "limit": 20}',
     'Feed: Trending, and the logged-out home feed'),
    ('bridge', 'get_ranked_posts', '{"sort": "hot", "tag": "", "limit": 20}', 'Feed: Hot'),
    ('bridge', 'get_ranked_posts', '{"sort": "promoted", "tag": "", "limit": 20}', 'Feed: Promoted'),
    ('bridge', 'get_ranked_posts', '{"sort": "created", "tag": "", "limit": 20}', 'Feed: New (bridge fallback)'),
    ('condenser_api', 'get_discussions_by_created', '[{"tag": "", "limit": 20}]', 'Feed: New'),
    ('bridge', 'get_ranked_posts', '{"sort": "trending", "tag": "", "observer": "", "limit": 6}',
     'Service worker: trending preview')
ON CONFLICT DO NOTHING;
