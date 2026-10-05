-- 007: Cost Control (COGS ratio, usage ratio, purchase forecast) per outlet.
--
-- Built by app/cost_control.py from integration_esb (engine integrated-esbapi):
--   inventory_valuation      ESB valuation per location x product x opname-aligned period
--   erp_documents / _lines   stock opname documents (pending = not yet posted in ESB)
--   pos_material_usage       theoretical material usage per branch x product x day
-- and the portal's own agg_sales_daily (net sales, subtotal).
--
-- Periods follow the outlet stock-opname rhythm: days 1-7, 8-14, 15-21, 22-end.
-- Values are ESB HPP (IDR). Sign convention here: costs and usage are POSITIVE,
-- opname variance is NEGATIVE for a loss (physical < system) and positive for a gain.

CREATE TABLE IF NOT EXISTS integration_portal.agg_cost_period (
    branch_code          text          NOT NULL,
    period_start         date          NOT NULL,
    period_end           date          NOT NULL,   -- running period: up to the last synced day
    location_id          text          NOT NULL,
    -- sales (ESB "Sales": Finished + bill number)
    bills                integer       NOT NULL DEFAULT 0,
    subtotal             numeric(18,2) NOT NULL DEFAULT 0,
    net_sales            numeric(18,2) NOT NULL DEFAULT 0,
    other_cost_subtotal  numeric(18,2) NOT NULL DEFAULT 0,   -- POS "other cost" bills (cupping, waste, ...) at menu price
    -- stock flows (HPP)
    begin_value          numeric(18,2) NOT NULL DEFAULT 0,
    purchase_value       numeric(18,2) NOT NULL DEFAULT 0,   -- goods received, net of returns
    transfer_in_value    numeric(18,2) NOT NULL DEFAULT 0,
    transfer_out_value   numeric(18,2) NOT NULL DEFAULT 0,
    theoretical_cogs     numeric(18,2) NOT NULL DEFAULT 0,   -- POS sales x BOM
    other_usage          numeric(18,2) NOT NULL DEFAULT 0,   -- item journal (waste, R&D, marketing, ...)
    manufacturing_net    numeric(18,2) NOT NULL DEFAULT 0,   -- materials used - products made (simple manufacturing)
    posted_variance      numeric(18,2) NOT NULL DEFAULT 0,   -- opname adjustments posted in ESB
    pending_variance     numeric(18,2) NOT NULL DEFAULT 0,   -- opname documents not posted yet (Draft/New)
    end_value            numeric(18,2) NOT NULL DEFAULT 0,   -- book value at period end
    actual_cogs          numeric(18,2) NOT NULL DEFAULT 0,   -- theoretical + other usage + manufacturing - variances
    opname_count         integer       NOT NULL DEFAULT 0,
    pending_opname_count integer       NOT NULL DEFAULT 0,
    last_opname_date     date,
    refreshed_at         timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_code, period_start)
);
CREATE INDEX IF NOT EXISTS agg_cost_period_start ON integration_portal.agg_cost_period (period_start);

CREATE TABLE IF NOT EXISTS integration_portal.agg_cost_item_period (
    branch_code          text          NOT NULL,
    period_start         date          NOT NULL,
    product_id           text          NOT NULL,
    product_code         text,
    product_name         text          NOT NULL,
    category             text,
    base_unit            text,
    begin_qty            numeric(20,6) NOT NULL DEFAULT 0,
    purchase_qty         numeric(20,6) NOT NULL DEFAULT 0,
    purchase_value       numeric(18,2) NOT NULL DEFAULT 0,
    theoretical_qty      numeric(20,6) NOT NULL DEFAULT 0,
    theoretical_value    numeric(18,2) NOT NULL DEFAULT 0,
    other_qty            numeric(20,6) NOT NULL DEFAULT 0,
    other_value          numeric(18,2) NOT NULL DEFAULT 0,
    variance_qty         numeric(20,6) NOT NULL DEFAULT 0,   -- posted + pending, negative = loss
    variance_value       numeric(18,2) NOT NULL DEFAULT 0,
    actual_qty           numeric(20,6) NOT NULL DEFAULT 0,   -- theoretical + other - variance (+ manufacturing)
    actual_value         numeric(18,2) NOT NULL DEFAULT 0,
    end_qty              numeric(20,6) NOT NULL DEFAULT 0,
    end_value            numeric(18,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (branch_code, period_start, product_id)
);
CREATE INDEX IF NOT EXISTS agg_cost_item_period_start ON integration_portal.agg_cost_item_period (period_start, branch_code);

-- Thresholds and options (editable; defaults from the F&B coffee-chain benchmarks in docs)
CREATE TABLE IF NOT EXISTS integration_portal.cost_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
INSERT INTO integration_portal.cost_settings (key, value) VALUES
  ('cogs_bands',     '{"good": 35, "warning": 40, "serious": 45}'),    -- % of net sales: <=35 good, <=40 watch, <=45 high, >45 critical
  ('usage_bands',    '{"good": 2, "warning": 5, "serious": 10}'),     -- |actual / theoretical - 100| in %
  ('variance_bands', '{"good": 1, "warning": 2, "serious": 3}'),      -- (actual - theoretical) COGS in %-points of net sales
  ('waste_bands',    '{"good": 1, "warning": 2, "serious": 3}'),      -- other usage in % of net sales
  ('forecast',       '{"lookback_days": 28, "safety_days": 2, "trend_cap_pct": 20}')
ON CONFLICT (key) DO NOTHING;
