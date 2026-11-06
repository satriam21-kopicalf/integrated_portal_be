-- 074: Tax / PB1 in the sales rollups (2026-11-06). ESB charges the tax on the subtotal of dine-in and takeaway bills:
-- vatTotal (most outlets) and otherTaxTotal (dine-in at some outlets, from 29 Oct 2026); tax_total is their sum per
-- bill (app/aggregates.py SQL_DAILY). Appended last: the daily and the cut comparison table keep the same column order
-- (app/routes/overview.py daily_source unions them with SELECT *). The history is filled by the rollup backfill.

-- taxed_bills: the bills with a tax (an outlet or channel without PB1 shows 0)
ALTER TABLE integration_portal.agg_sales_daily ADD COLUMN IF NOT EXISTS tax_total numeric(18,2) NOT NULL DEFAULT 0;
ALTER TABLE integration_portal.agg_sales_daily ADD COLUMN IF NOT EXISTS taxed_bills integer NOT NULL DEFAULT 0;
ALTER TABLE integration_portal.agg_cmp_daily ADD COLUMN IF NOT EXISTS tax_total numeric(18,2) NOT NULL DEFAULT 0;
ALTER TABLE integration_portal.agg_cmp_daily ADD COLUMN IF NOT EXISTS taxed_bills integer NOT NULL DEFAULT 0;
