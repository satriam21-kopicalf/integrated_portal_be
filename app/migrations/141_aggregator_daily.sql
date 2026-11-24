-- 141: Aggregator Insight (2026-11-24): BotFood's own daily availability report of every outlet and platform
-- (me/report/atp?date=, made around 02:00-04:00 for the day before, available back to January 2026): opening and closing
-- state (on_time / on_time_botfood / early / late / not_open), the open and closed stretches, breaks and the share of
-- items available. Opening & Closing reads it for every past day; today is worked out live from the change events.
CREATE TABLE IF NOT EXISTS integration_portal.agg_daily (
    channel_id         bigint NOT NULL,
    day                date NOT NULL,
    bf_branch_id       bigint,
    name               text,
    channel            text,
    open_time          time,
    close_time         time,
    open_state         text,
    close_state        text,
    has_break          boolean,
    active_s           integer,
    inactive_s         integer,
    hours_s            integer,
    timeline           text,
    inactive_timeline  text,
    items_pct          numeric(6, 3),
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, day)
);
CREATE INDEX IF NOT EXISTS agg_daily_day ON integration_portal.agg_daily (day);

-- the daily report filled back from 1 January; the review backfill starts again (until 141 a one-day range asked
-- BotFood for nothing, so the days it passed were kept empty)
INSERT INTO integration_portal.agg_state (key, value) VALUES ('daily_backfill_day', '2026-11-23') ON CONFLICT DO NOTHING;
UPDATE integration_portal.agg_state SET value = '2026-11-23' WHERE key = 'review_backfill_day';
