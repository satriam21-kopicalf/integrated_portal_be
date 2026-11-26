-- 158: SO Daily auto-submit (2026-11-26, app/so_autosubmit.py): the outlets that did no SO Daily on a day, noted at 07:00 WIB
-- the morning after (no empty count is made: it would read as stock 0).
CREATE TABLE IF NOT EXISTS integration_portal.so_daily_missing (
    day         date        NOT NULL,
    branch_code text        NOT NULL,
    name        text,
    noted_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, branch_code)
);
