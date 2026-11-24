-- 140: Aggregator Insight stage 2 (2026-11-24): the change events BotFood keeps of the day (me/histories: every open /
-- close of an outlet on a platform with its opening hours, every item going out of / back in stock with a remark),
-- pulled every 15 minutes and kept here for good — BotFood only answers today. The times are when BotFood saw the
-- change (its own delay), so Opening & Closing is "according to BotFood", with a grace period.
ALTER TABLE integration_portal.agg_channel ADD COLUMN IF NOT EXISTS open_time time;
ALTER TABLE integration_portal.agg_channel ADD COLUMN IF NOT EXISTS close_time time;

CREATE TABLE IF NOT EXISTS integration_portal.agg_status_event (
    id          bigint PRIMARY KEY,                  -- BotFood history id
    channel_id  bigint NOT NULL,
    at          timestamptz NOT NULL,
    is_open     boolean NOT NULL,
    open_time   time,
    close_time  time,
    is_warning  boolean
);
CREATE INDEX IF NOT EXISTS agg_status_event_channel ON integration_portal.agg_status_event (channel_id, at);
CREATE INDEX IF NOT EXISTS agg_status_event_at ON integration_portal.agg_status_event (at);

CREATE TABLE IF NOT EXISTS integration_portal.agg_stock_event (
    id          bigint PRIMARY KEY,                  -- BotFood history id
    item_id     bigint,
    channel_id  bigint NOT NULL,
    item_name   text NOT NULL,
    in_stock    boolean NOT NULL,
    remarks     text,
    at          timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS agg_stock_event_at ON integration_portal.agg_stock_event (at);
CREATE INDEX IF NOT EXISTS agg_stock_event_item ON integration_portal.agg_stock_event (channel_id, item_name, at);

-- the settings of the reports: minutes of grace before an opening counts as late or a closing as early
INSERT INTO integration_portal.agg_state (key, value) VALUES ('grace_minutes', '15') ON CONFLICT DO NOTHING;
