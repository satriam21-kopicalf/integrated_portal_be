-- 154: Aggregator Insight (2026-11-25): the opening hours of the merchant app are the reference. BotFood's daily report
-- gives them per outlet, platform and day (agg_daily.hours, e.g. '08:00 - 11:45; 12:45 - 21:00'); the Aggregator team checks
-- them in the merchant app (agg_hours_check): "ok" = the same, "diff" = the hours the app really shows, from a date on.
ALTER TABLE integration_portal.agg_daily ADD COLUMN IF NOT EXISTS hours text;

CREATE TABLE IF NOT EXISTS integration_portal.agg_hours_check (
    branch_code text NOT NULL,
    channel     text NOT NULL,
    valid_from  date NOT NULL,
    status      text NOT NULL CHECK (status IN ('ok', 'diff')),
    open_time   time,
    close_time  time,
    app_hours   text,
    by_name     text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_code, channel, valid_from),
    CHECK (status = 'ok' OR (open_time IS NOT NULL AND close_time IS NOT NULL))
);

-- read the daily reports again, newest first, for their hours
UPDATE integration_portal.agg_state SET value = to_char((now() AT TIME ZONE 'Asia/Jakarta')::date - 1, 'YYYY-MM-DD') WHERE key = 'daily_backfill_day';
