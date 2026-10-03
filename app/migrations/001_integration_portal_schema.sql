-- 001: schema owned by integrated_portal_be / integrated_portal.
--
-- Daily aggregates of the POS sales transactions in integration_esb (the ESB
-- sync engine's schema, read-only for the portal). They power the Overview
-- page: aggregating raw_data on the fly costs 40-90 s per month, these tables
-- answer in milliseconds. Rebuilt per sales date by app/aggregates.py.
-- Only fields that are 100% filled across the whole history are used
-- (see integrated_portal/docs/overview-analytics.md §2).

CREATE SCHEMA IF NOT EXISTS integration_portal;

-- Sales per day x branch x channel x payment method x transaction type.
-- tx_type: sales (ESB "Sales": Finished + bill number) | void (Void/Cancelled)
--          | other_cost (Finished, no bill number: CUPPING, WASTE, ...) | open
CREATE TABLE IF NOT EXISTS integration_portal.agg_sales_daily (
    sales_date          date        NOT NULL,
    branch_code         text        NOT NULL,
    channel             text        NOT NULL,   -- raw_data.visitPurposeName
    payment_type        text        NOT NULL,   -- salesPayments[0].paymentMethodTypeName
    payment_method      text        NOT NULL,   -- salesPayments[0].paymentMethodName
    tx_type             text        NOT NULL,
    bills               integer     NOT NULL,
    subtotal            numeric(18,2) NOT NULL,
    nett_sales          numeric(18,2) NOT NULL,
    grand_total         numeric(18,2) NOT NULL,
    discount_total      numeric(18,2) NOT NULL, -- bill discount (discountTotal)
    menu_discount       numeric(18,2) NOT NULL, -- menuDiscountTotal
    promotion_discount  numeric(18,2) NOT NULL,
    voucher_discount    numeric(18,2) NOT NULL,
    menu_lines          integer     NOT NULL,   -- ordered menu lines (excl. packages/extras)
    item_qty            numeric(18,2) NOT NULL, -- qty of ordered menus
    bills_with_beverage integer     NOT NULL,
    bills_with_food     integer     NOT NULL,
    bills_with_both     integer     NOT NULL,
    PRIMARY KEY (sales_date, branch_code, channel, payment_type, payment_method, tx_type)
);
CREATE INDEX IF NOT EXISTS agg_sales_daily_type_date ON integration_portal.agg_sales_daily (tx_type, sales_date);

-- ESB sales per day x branch x channel x hour of salesDateIn (WIB wall clock).
CREATE TABLE IF NOT EXISTS integration_portal.agg_sales_hourly (
    sales_date   date     NOT NULL,
    branch_code  text     NOT NULL,
    channel      text     NOT NULL,
    hour         smallint NOT NULL,
    bills        integer  NOT NULL,
    subtotal     numeric(18,2) NOT NULL,
    PRIMARY KEY (sales_date, branch_code, channel, hour)
);

-- ESB sales per day x branch x channel x menu (menus, packages and extras).
CREATE TABLE IF NOT EXISTS integration_portal.agg_menu_daily (
    sales_date      date  NOT NULL,
    branch_code     text  NOT NULL,
    channel         text  NOT NULL,
    menu_id         text  NOT NULL,
    kind            text  NOT NULL,   -- menu | package | extra
    menu_name       text  NOT NULL,
    category        text  NOT NULL,
    category_detail text  NOT NULL,
    bills           integer NOT NULL,
    qty             numeric(18,2) NOT NULL,
    subtotal        numeric(18,2) NOT NULL,  -- price x qty
    discount        numeric(18,2) NOT NULL,  -- item discountValue
    PRIMARY KEY (sales_date, branch_code, channel, menu_id, kind)
);
CREATE INDEX IF NOT EXISTS agg_menu_daily_date ON integration_portal.agg_menu_daily (sales_date);

-- One row per rebuilt sales date (freshness + reconciliation).
CREATE TABLE IF NOT EXISTS integration_portal.agg_refresh_log (
    sales_date      date        PRIMARY KEY,
    refreshed_at    timestamptz NOT NULL DEFAULT now(),
    source_synced_at timestamptz,            -- latest synced_at of that day's source rows
    sales_bills     integer     NOT NULL,
    sales_subtotal  numeric(18,2) NOT NULL,
    duration_ms     integer     NOT NULL
);
