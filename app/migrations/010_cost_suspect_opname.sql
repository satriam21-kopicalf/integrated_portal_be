-- 010: Cost Control - implausible lines of unposted stock opnames.
--
-- A line of a Draft/New opname whose variance is larger than max(Rp 50 M, 50% of the
-- outlet's theoretical COGS of the period) is left out of the pending variance (and so out of
-- actual COGS), e.g. a system stock of 29 tonnes of espresso; it is listed under
-- /api/cost-control/issues so the outlet / ops can correct it in ESB. Posted opnames are
-- never excluded (they are already in the ESB stock valuation).

ALTER TABLE integration_portal.agg_cost_period
    ADD COLUMN IF NOT EXISTS excluded_pending_variance numeric NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS excluded_pending_lines integer NOT NULL DEFAULT 0;
