-- ============================================================================
-- Corridor Proxy — labelled venue-to-venue stablecoin flows (90 days)
--
-- Paste into the Dune UI, save, and put the resulting query id in
-- config.json as corridor.dune_query_id. scripts/fetch_dune.py reads the
-- saved results; it never sends SQL.
--
-- Output: one row per (from_venue, to_venue, token, day)
--   from_venue      cex.addresses label of the sending address
--   to_venue        cex.addresses label of the receiving address
--   token           USDT | USDC
--   day             UTC date of the transfer
--   usd_volume      sum of amount_usd
--   transfer_count  number of transfers
--
-- What this is NOT
--   Both ends must already be labelled exchange addresses, so this measures
--   exchange-to-exchange settlement, not user remittance. A user cashing out
--   to a bank, or moving to a self-custody wallet, is invisible here. Read
--   METHODOLOGY.md before drawing a conclusion from any number it produces.
--
-- Coverage caveats worth knowing before you trust a venue's totals
--   * Dune's token transfer coverage is EVM chains plus Solana. Tron is the
--     single largest USDT retail rail in exactly the markets this module cares
--     about, and it is not in here. Treat every regional total as a floor.
--   * cex.addresses is community-maintained. Large global venues are labelled
--     well; regional venues are labelled patchily or not at all. An absent
--     venue looks identical to a venue with no flow.
--   * Venues net internally and settle in batches. One onchain transfer can
--     represent thousands of user transactions, or none at all (treasury
--     rebalancing between a venue's own hot wallets on different exchanges).
-- ============================================================================

WITH params AS (
    SELECT
        90                AS lookback_days,
        CAST(1 AS DOUBLE) AS min_transfer_usd,       -- drop dust and airdrop spam
        CAST(5e8 AS DOUBLE) AS max_transfer_usd      -- drop obvious price-feed glitches
),

-- cex.addresses can carry more than one row per address (different curators,
-- different distinct_name for the same venue). Collapse to one label per
-- (chain, address) so the joins below cannot fan out and double-count volume.
venues AS (
    SELECT
        blockchain,
        address,
        MAX(cex_name) AS venue
    FROM cex.addresses
    WHERE cex_name IS NOT NULL
      AND cex_name <> ''
    GROUP BY blockchain, address
),

stable_transfers AS (
    SELECT
        t.block_date AS day,
        t.blockchain,
        t.symbol     AS token,
        t."from"     AS from_address,
        t."to"       AS to_address,
        t.amount_usd
    FROM tokens.transfers t
    CROSS JOIN params p
    -- block_month first: it is the partition key, and pruning on it is the
    -- difference between a cheap query and an expensive one.
    WHERE t.block_month >= date_trunc('month', date_add('day', -p.lookback_days, now()))
      AND t.block_time  >= date_add('day', -p.lookback_days, now())
      AND t.symbol IN ('USDT', 'USDC')
      AND t.amount_usd BETWEEN p.min_transfer_usd AND p.max_transfer_usd
)

SELECT
    src.venue                      AS from_venue,
    dst.venue                      AS to_venue,
    x.token                        AS token,
    x.day                          AS day,
    ROUND(SUM(x.amount_usd), 2)    AS usd_volume,
    COUNT(*)                       AS transfer_count
FROM stable_transfers x
INNER JOIN venues src
        ON src.blockchain = x.blockchain
       AND src.address    = x.from_address
INNER JOIN venues dst
        ON dst.blockchain = x.blockchain
       AND dst.address    = x.to_address
-- Intra-venue transfers are wallet housekeeping, not a flow between markets.
WHERE src.venue <> dst.venue
GROUP BY src.venue, dst.venue, x.token, x.day
HAVING SUM(x.amount_usd) > 0
ORDER BY x.day DESC, usd_volume DESC
