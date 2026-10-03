-- 003: monthly rollup of agg_sales_hourly by weekday.
--
-- agg_sales_hourly holds ~4.5k rows per day (1.6M per year). The busy-hours
-- heatmap only needs weekday x hour, so full calendar months are read from this
-- table and only the partial edge days from agg_sales_hourly. Rebuilt per month
-- by app/aggregates.py together with agg_menu_monthly.

CREATE TABLE IF NOT EXISTS integration_portal.agg_hourly_monthly (
    month        date     NOT NULL,   -- first day of the month
    branch_code  text     NOT NULL,
    channel      text     NOT NULL,
    dow          smallint NOT NULL,   -- ISO weekday, 1 = Monday
    hour         smallint NOT NULL,
    bills        integer  NOT NULL,
    subtotal     numeric(18,2) NOT NULL,
    PRIMARY KEY (month, branch_code, channel, dow, hour)
);
