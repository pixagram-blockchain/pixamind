# Post and comment history

Hivemind keeps only the latest title, body and json_metadata of a post. Pixamind keeps
every version: the creation and each edit of every post and comment, queryable with
`pixamind_content.get_post_history`.

On by default (`--pixamind-post-history` / `PIXAMIND_POST_HISTORY`). A node that
indexed posts before recording their history runs `pixamind history backfill` once.

## The API

`pixamind_content.get_post_history`
`{"author": "bob", "permlink": "blog-1", "start": 1, "limit": 20, "bodies": true}`
(`start`: first version number, default 1; `limit` ≤ 100, default 20; `bodies: false`
leaves out the bodies, for a list of versions):

```json
{"author": "bob", "permlink": "blog-1", "total": 3, "complete": true,
 "versions": [
   {"version": 1, "block_num": 2, "timestamp": "2026-09-01T00:00:03", "op_id": "8589935361",
    "created": true, "changed": ["title", "body", "json_metadata"],
    "title": "Hello", "body": "First post", "json_metadata": "{\"app\": \"pixagram\", \"format\": \"text\", \"tags\": [\"blog\"]}"},
   {"version": 2, "block_num": 9, "timestamp": "2026-09-01T00:00:24", "op_id": "38654705921",
    "created": false, "changed": ["body"], "title": "Hello", "body": "Final post", "json_metadata": "…"},
   {"version": 3, "block_num": 9, "timestamp": "2026-09-01T00:00:24", "op_id": "38654706177",
    "created": false, "changed": ["json_metadata"], "title": "Hello", "body": "Final post",
    "json_metadata": "{\"app\": \"pixagram\", \"tags\": [\"blog\", \"news\"]}"}]}
```

* Each version is the post as it was after one comment operation, in full: title, body
  and json_metadata (the string the author sent, as condenser_api returns it).
* `changed`: the fields that differ from the previous version (all three for the
  creation; `[]` for an edit that changed nothing; `null` when the previous version is
  not known, see below).
* `timestamp` follows hivemind's convention for post times, so the first and last
  versions match the post's `created` and `updated`. `op_id` is the HAF operation id
  (a string: it exceeds 2^53), from which the block and the transaction can be found.
* `complete` is false when the history does not start with the creation: on a node that
  indexed the post before recording history, until the backfill has run.
* The post is the live one: after `delete_comment` and a new post with the same
  permlink, the history starts again with the new post. A deleted post answers the usual
  "was deleted" error.

The last version is always what hivemind serves for the post (`get_content`,
`get_discussion`, …).

## How it is recorded

An edit sends either the full new body or a diff-match-patch patch that hivemind applies
to the stored body (hive/indexer/posts.py). Pixamind records each version right where
hivemind computes it: the block processor hands every comment operation of a batch,
merged, to `pixamind/indexer/history.py`, which writes `pixamind.post_versions`:

* live sync: inside the block's transaction, under a savepoint (the versions commit with
  the block; an error here never aborts it);
* massive sync: in a transaction of its own, before hivemind stores the batch's post data;
  every operation of the batch gets its version, even several edits of a post in one
  batch.

Nothing is stored twice:

| Row | Stored text |
|---|---|
| creation of a post never edited | none: the text is hivemind's (`hive_post_data`) |
| creation of an edited post | the original title, body and json_metadata (written at the first edit) |
| edit | only the fields it changed; a new title does not store the artwork again |
| first edit seen of a post created before history was recorded | complete, until the backfill adds what came before |

`pixamind.post_version_states(post_id)` gives every version in full (what the API reads).

A failure is recorded in `pixamind.projection_failures` (`post_versions`) and the indexer
goes on; `pixamind history backfill --from-block N` repairs it.

## Backfill

```bash
pixamind history backfill --database-url postgresql://hivemind@localhost:5432/haf_block_log
```

Rebuilds the versions of every live post and comment from the comment operations in HAF,
each post from its creation, merging edits exactly like hivemind (same library, same
rules), up to the block the indexer has processed. Versions already recorded are kept,
so it is idempotent and safe while the indexer runs. It ends by comparing the latest
version of every rebuilt post with what hivemind stores, and exits 1 if one differs.
After a complete run `get_info.features.post_history_backfilled` is true.

`--from-block N` rebuilds only the posts with an operation from block N on (still from
their creation); `--posts` sets how many posts go in one transaction (default 500).
