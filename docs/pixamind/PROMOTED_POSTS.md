# Promoted posts

Paid promotion is back: anyone can push a post up the **Promoted** feed by burning the
stable token for it.

## How it works

1. A user sends PXS to the `null` account with the memo `@author/permlink`:

   ```json
   ["transfer", {"from": "carol", "to": "null", "amount": "1.500 PXS", "memo": "@alice/cat"}]
   ```

2. The chain burns what `null` receives — no consensus change is needed: Hive and Pixa
   accept transfers to `null` and clear its balance. Steem's promotion worked exactly this
   way; Hive removed only the **indexing** from hivemind (January 2025), and Pixamind
   restores it.
3. Pixamind records the payment and adds it to the post's promoted total.
4. The post appears in the promoted feed, ranked by total, until its payout.

Rules (as in Steem's hivemind): only the stable token counts (PXS, NAI `@@000000013`);
the memo must be exactly `@author/permlink`; the post must exist (payments for missing
posts are recorded but rank nothing); paid-out posts leave the feed. Payments are never
refunded — the funds are burned.

## Reading it

| Method | |
|---|---|
| `bridge.get_ranked_posts` `{"sort": "promoted", "tag": …, "observer": …}` | the feed, bridge post objects with `"promoted": "1.750 PXS"`; `tag` can be a tag, a community (`portal-…`) or empty |
| `condenser_api.get_discussions_by_promoted` | the same feed for condenser-style clients |
| `pixamind_feed.get_promoted_posts` | alias of the bridge call |
| `pixamind_feed.get_promotions` `{"author", "permlink"}` / `{"sender"}` | payments behind a post / spending of an account |
| `pixamind_api.get_events` `{"types": ["promotion"]}` | promotions as they happen |

The observer's mutes apply, as in other feeds. Paging uses `start_author`/`start_permlink`.

## In the Pixagram UI

A "Promote" action needs only a transfer signed with the active key — with the web
app's API (`src/js/utils/api/pixaproxyapi.js`), which already exposes it:

```js
await api.broadcast.transfer(account, 'null', `${amount.toFixed(3)} PXS`, `@${author}/${permlink}`);
```

and a **Promoted** tab reading `bridge.get_ranked_posts({sort: 'promoted', tag, observer})`.
The ad-density rules of the Pixagram feeds decide where promoted posts show up; Pixamind
only ranks them.

## Implementation

* `pixamind/sql/runtime/50_promotions.sql` — `process_promotions(first, last)` reads
  transfers to `null` from HAF (`operations_view`, using HAF's op-type index), writes
  `pixamind.post_promotions`, recomputes `pixamind.promoted_posts` for the touched posts;
  `get_promoted_ranked_posts(...)` builds the feed with hivemind's post objects.
* Runs in phase 8 of every batch/block; `--pixamind-promotions=false` turns it off.
* Re-processing a range (crash replay, micro-fork) replaces its payments and recomputes
  the totals, so the feed converges to the chain.
