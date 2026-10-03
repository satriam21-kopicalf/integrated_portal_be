-- 002: monthly rollup of agg_menu_daily.
--
-- agg_menu_daily holds ~10k rows per day; a one-year menu query aggregates
-- millions of rows. The Overview reads full calendar months from this table
-- and only the partial edge days from agg_menu_daily. Rebuilt per month by
-- app/aggregates.py after the days of that month are refreshed.

CREATE TABLE IF NOT EXISTS integration_portal.agg_menu_monthly (
    month           date  NOT NULL,   -- first day of the month
    branch_code     text  NOT NULL,
    channel         text  NOT NULL,
    menu_id         text  NOT NULL,
    kind            text  NOT NULL,   -- menu | package | extra
    menu_name       text  NOT NULL,
    category        text  NOT NULL,
    category_detail text  NOT NULL,
    bills           integer NOT NULL,
    qty             numeric(18,2) NOT NULL,
    subtotal        numeric(18,2) NOT NULL,
    discount        numeric(18,2) NOT NULL,
    PRIMARY KEY (month, branch_code, channel, menu_id, kind)
);
