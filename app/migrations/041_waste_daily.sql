-- 041: Waste every day (2026-10-31, user request). Usage Management becomes a full list: the branch fills the quantity of
-- every usage item (waste_item scope 'usage') once a day, saved again to correct it (one row per branch, day and item).
-- Every branch reports its waste daily: a waste entry, or "no waste today" (waste_day.no_waste); the monitor lists the
-- branches that have not reported (app/waste.py daily()).

CREATE TABLE IF NOT EXISTS integration_portal.usage_count (
    branch_code   text           NOT NULL,
    branch_name   text           NOT NULL,
    business_date date           NOT NULL,
    item_code     text           NOT NULL,
    item_name     text           NOT NULL,
    unit          text           NOT NULL,
    qty           numeric(14, 3) NOT NULL CHECK (qty >= 0),
    user_id       uuid           NOT NULL,
    username      text           NOT NULL,
    full_name     text,
    updated_at    timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_code, business_date, item_code)
);
CREATE INDEX IF NOT EXISTS usage_count_day_idx ON integration_portal.usage_count (business_date DESC);

CREATE TABLE IF NOT EXISTS integration_portal.waste_day (
    branch_code   text        NOT NULL,
    business_date date        NOT NULL,
    branch_name   text        NOT NULL,
    no_waste      boolean     NOT NULL DEFAULT true,
    user_id       uuid        NOT NULL,
    username      text        NOT NULL,
    full_name     text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_code, business_date)
);
