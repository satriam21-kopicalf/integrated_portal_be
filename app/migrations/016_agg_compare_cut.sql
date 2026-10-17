-- Like-for-like comparison for today (user decision 2026-10-17): the Overview compares today with the same weekday
-- last week. Today is still running, so the comparison day is cut at the time of today's last bill. These tables hold
-- that cut: the same weekday 1-5 weeks back (today-7 .. today-35), rebuilt with today's aggregates (app/aggregates.py).
CREATE TABLE IF NOT EXISTS integration_portal.agg_cmp_daily (LIKE integration_portal.agg_sales_daily INCLUDING DEFAULTS);
CREATE TABLE IF NOT EXISTS integration_portal.agg_cmp_menu (LIKE integration_portal.agg_menu_daily INCLUDING DEFAULTS);
CREATE INDEX IF NOT EXISTS agg_cmp_daily_date ON integration_portal.agg_cmp_daily (sales_date, tx_type);
CREATE INDEX IF NOT EXISTS agg_cmp_menu_date ON integration_portal.agg_cmp_menu (sales_date);
CREATE TABLE IF NOT EXISTS integration_portal.agg_cmp_meta (
    sales_date  date        PRIMARY KEY,   -- the comparison day (same weekday as today, whole weeks back)
    for_day     date        NOT NULL,      -- the running day it is cut for (today)
    cut_time    time        NOT NULL,      -- salesDateIn of today's last bill (WIB wall clock)
    built_at    timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE integration_portal.agg_cmp_meta IS
    'cut of agg_cmp_daily / agg_cmp_menu: sales_date up to cut_time, for comparing the running day for_day like for like';
