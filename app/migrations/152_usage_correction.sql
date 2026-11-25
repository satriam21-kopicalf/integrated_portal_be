-- 152: Cost Control corrects a Usage Management line (2026-11-25, Cost Control request: quantities entered in pieces or
-- grams instead of packs). The line is changed in place; every change is kept with its old and new value and the reason.
CREATE TABLE IF NOT EXISTS integration_portal.usage_correction (
    id            bigserial   PRIMARY KEY,
    branch_code   text        NOT NULL,
    business_date date        NOT NULL,
    item_code     text        NOT NULL,
    field         text        NOT NULL,   -- qty | item | category | note
    old_value     text,
    new_value     text,
    reason        text        NOT NULL,
    by_name       text        NOT NULL,
    by_user       uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (branch_code, business_date, item_code)
        REFERENCES integration_portal.usage_count (branch_code, business_date, item_code) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS usage_correction_line_idx ON integration_portal.usage_correction (branch_code, business_date, item_code);
CREATE INDEX IF NOT EXISTS usage_correction_at_idx ON integration_portal.usage_correction (created_at);
