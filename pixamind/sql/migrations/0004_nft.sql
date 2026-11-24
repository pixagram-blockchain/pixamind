-- Pixamind 0004: native NFTs (Pixa chain hardfork 32).
--
-- A post becomes an NFT through the chain's nft_operation (operation id 37), with 1 to 10,000 copies
-- (editions) numbered from 1; a sale is reported by the virtual operation nft_sold_operation (id 93).
-- Prices, royalties and the mint fee (1 per copy) are in {{HBD_SYMBOL}}, and a copy given to the null
-- account is burned. The license is the full text of the terms, up to 72 KiB: the chain keeps only its
-- SHA-256, Pixamind keeps the text too, so that it can be read without the block log.
--
-- On chain a copy has state of its own only once it was offered or moved; until then it is its
-- creator's, acquired at mint, not offered. Pixamind follows the same rule: pixamind.nft_editions has
-- a row for a copy once something happened to it, and every reader takes a missing row for the
-- creator's untouched copy. A mint of 10,000 copies therefore adds one row, not 10,000.
--
-- The copies without a row are the reserve. The creator can offer the whole reserve at one price (an
-- offer of copy 0); a buyer of copy 0 gets the lowest-numbered copy still in the reserve, which the
-- sale names. When the creator withdraws the offer of a copy that never left him, the chain removes
-- its state and Pixamind its row: the copy is back in the reserve.
--
-- The chain keeps the current state only. These tables add what a marketplace needs on top of it - the
-- history, and lists by creator, by owner and by price. Like every Pixamind table they are derived
-- data, rebuilt from the chain by pixamind.process_nfts (runtime/55_nft.sql).

-- Every NFT action that changed chain state, in chain order. This is the owner history, and the
-- two tables below are folded from it.
CREATE TABLE IF NOT EXISTS pixamind.nft_events (
    op_id          BIGINT PRIMARY KEY,                -- HAF operation id: chain order, same on every node
    block_num      INT NOT NULL,
    created_at     TIMESTAMP NOT NULL,
    type           TEXT NOT NULL CHECK (type IN ('mint', 'list', 'unlist', 'sale', 'transfer', 'burn')),
    author         VARCHAR(16) COLLATE "C" NOT NULL,  -- the NFT: author and permlink of the minted post
    permlink       VARCHAR(255) COLLATE "C" NOT NULL,
    edition        INT NOT NULL,                      -- the copy; 0 for the mint and for the offer of the
                                                      -- reserve (list, unlist), which concern the NFT
    account        VARCHAR(16) COLLATE "C" NOT NULL,  -- who acted: the minter, the owner listing, unlisting,
                                                      -- giving or burning, the buyer
    counterparty   VARCHAR(16) COLLATE "C",           -- sale: the seller; transfer: the receiver; burn: null
    price          NUMERIC(20, 3),                    -- list: asking price; sale: price paid
    royalty        NUMERIC(20, 3),                    -- sale: the part of the price paid to the creator
    fee            NUMERIC(20, 3),                    -- mint: the fee burned, 1 per copy
    expiration     TIMESTAMP,                         -- list: the offer cannot be taken at or after this time
    editions       INT,                               -- mint: number of copies
    royalty_bps    INT,                               -- mint: royalty in basis points
    license        TEXT,                              -- mint: the full license text, as the operation carries it
    license_sha256 TEXT                               -- mint: SHA-256 of that text in hex, as the chain keeps it
);
CREATE INDEX IF NOT EXISTS nft_events_block_num_idx ON pixamind.nft_events (block_num);
CREATE INDEX IF NOT EXISTS nft_events_copy_idx ON pixamind.nft_events (author, permlink, edition, op_id);
CREATE INDEX IF NOT EXISTS nft_events_nft_idx ON pixamind.nft_events (author, permlink, op_id);
CREATE INDEX IF NOT EXISTS nft_events_account_idx ON pixamind.nft_events (account, op_id);
CREATE INDEX IF NOT EXISTS nft_events_counterparty_idx ON pixamind.nft_events (counterparty, op_id) WHERE counterparty IS NOT NULL;

-- One row per minted post: the terms fixed at mint, which the chain never changes, and the reserve.
CREATE TABLE IF NOT EXISTS pixamind.nfts (
    author         VARCHAR(16) COLLATE "C" NOT NULL,
    permlink       VARCHAR(255) COLLATE "C" NOT NULL,
    post_id        INT,                               -- hivemind_app.hive_posts.id; NULL when hivemind has no such post
    mint_op_id     BIGINT NOT NULL,
    mint_block     INT NOT NULL,
    minted_at      TIMESTAMP NOT NULL,
    editions       INT NOT NULL,
    royalty_bps    INT NOT NULL,
    license_size   INT NOT NULL,                      -- bytes of the license text (the text: nft_events, the mint)
    license_sha256 TEXT NOT NULL,
    mint_fee       NUMERIC(20, 3) NOT NULL,
    reserve        INT NOT NULL DEFAULT 0,            -- copies without a row in nft_editions
    reserve_price  NUMERIC(20, 3),                    -- the creator's price for one copy of the reserve;
    reserve_expiration TIMESTAMP,                     -- NULL when the reserve is not offered (an expired
    reserve_listed_op_id BIGINT,                      -- offer stays, as on chain)
    reserve_listed_at TIMESTAMP,
    PRIMARY KEY (author, permlink)
);
CREATE INDEX IF NOT EXISTS nfts_creator_idx ON pixamind.nfts (author, mint_op_id DESC);
CREATE INDEX IF NOT EXISTS nfts_minted_idx ON pixamind.nfts (mint_op_id DESC);
CREATE INDEX IF NOT EXISTS nfts_post_id_idx ON pixamind.nfts (post_id) WHERE post_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS nfts_reserve_price_idx ON pixamind.nfts (reserve_price, reserve_listed_op_id) WHERE reserve_price IS NOT NULL;
CREATE INDEX IF NOT EXISTS nfts_reserve_recent_idx ON pixamind.nfts (reserve_listed_op_id DESC) WHERE reserve_price IS NOT NULL;

-- One row per copy that was offered or moved at least once: the current owner, the current offer and
-- what the copy sold for so far. A copy without a row is its creator's, in the reserve.
-- An offer whose expiration has passed stays here, as it does on chain; readers compare it with the
-- head block time. A burned copy stays too, owned by null.
CREATE TABLE IF NOT EXISTS pixamind.nft_editions (
    author           VARCHAR(16) COLLATE "C" NOT NULL,
    permlink         VARCHAR(255) COLLATE "C" NOT NULL,
    edition          INT NOT NULL,
    owner            VARCHAR(16) COLLATE "C" NOT NULL,  -- 'null' once the copy is burned
    acquired_op_id   BIGINT NOT NULL,                   -- the mint (still the creator's), sale, transfer or burn
                                                        -- that made `owner` the owner
    acquired_at      TIMESTAMP NOT NULL,
    price            NUMERIC(20, 3),                    -- asking price; NULL when the copy is not offered
    expiration       TIMESTAMP,
    listed_op_id     BIGINT,
    listed_at        TIMESTAMP,
    sales            INT NOT NULL DEFAULT 0,
    volume           NUMERIC(20, 3) NOT NULL DEFAULT 0, -- sum of the sale prices
    royalties        NUMERIC(20, 3) NOT NULL DEFAULT 0, -- sum of the royalties paid to the creator
    last_sale_op_id  BIGINT,
    last_sale_price  NUMERIC(20, 3),
    last_sale_at     TIMESTAMP,
    PRIMARY KEY (author, permlink, edition)
);
CREATE INDEX IF NOT EXISTS nft_editions_owner_idx ON pixamind.nft_editions (owner, acquired_op_id DESC);
CREATE INDEX IF NOT EXISTS nft_editions_offer_price_idx ON pixamind.nft_editions (price, listed_op_id) WHERE price IS NOT NULL;
CREATE INDEX IF NOT EXISTS nft_editions_offer_recent_idx ON pixamind.nft_editions (listed_op_id DESC) WHERE price IS NOT NULL;
CREATE INDEX IF NOT EXISTS nft_editions_nft_offer_idx ON pixamind.nft_editions (author, permlink, price, listed_op_id) WHERE price IS NOT NULL;
