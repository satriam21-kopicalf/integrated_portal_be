-- 123: Stock List Transit In / Out (2026-11-19, Cost Control): goods on the way between a branch and another place that
-- no ESB document or portal transfer carries yet. Cost Control enters them by hand; the monthly Stock List adds them
-- to Items In / Items Out of the branch. Removed rows are kept (deleted_at) for the record.

CREATE TABLE IF NOT EXISTS integration_portal.cc_stock_transit (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_code   text NOT NULL,
    item_code     text NOT NULL,
    unit          text NOT NULL,
    qty           numeric NOT NULL CHECK (qty > 0),
    direction     text NOT NULL CHECK (direction IN ('in', 'out')),
    business_date date NOT NULL,
    note          text,
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz,
    deleted_by    text
);
CREATE INDEX IF NOT EXISTS cc_stock_transit_branch_day ON integration_portal.cc_stock_transit (branch_code, business_date) WHERE deleted_at IS NULL;
