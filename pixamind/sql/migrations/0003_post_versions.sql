-- Pixamind 0003: every version of every post and comment.
--
-- Hivemind keeps only the latest title, body and json_metadata of a post
-- (hivemind_app.hive_post_data). Pixamind records the state after each comment
-- operation: the creation, then every edit, with the body exactly as hivemind
-- merged it (an edit may send a diff-match-patch patch instead of the body).
-- Served by pixamind_content.get_post_history (docs/pixamind/POST-HISTORY.md).
--
-- Nothing is stored twice:
-- * the creation row keeps NULL columns while the post was never edited (its
--   text is the one in hive_post_data); the first edit fills them with the
--   original text;
-- * an edit stores only the columns it changed (NULL for the others), so a new
--   title does not store the artwork again;
-- * for a post created before Pixamind recorded history, the first edit seen is
--   stored complete, until `pixamind history backfill` adds the older versions.
CREATE TABLE IF NOT EXISTS pixamind.post_versions (
    op_id         BIGINT PRIMARY KEY,  -- HAF id of the comment operation
    post_id       INT NOT NULL,        -- hivemind_app.hive_posts.id
    block_num     INT NOT NULL,
    created_at    TIMESTAMP NOT NULL,  -- block time
    is_creation   BOOLEAN NOT NULL,    -- the operation created the post
    title         TEXT,
    body          TEXT,
    json_metadata TEXT
);
CREATE INDEX IF NOT EXISTS post_versions_post_id_op_id_idx ON pixamind.post_versions (post_id, op_id);
