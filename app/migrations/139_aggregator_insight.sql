-- 139: Aggregator Insight (2026-11-24), stage 1: the BotFood data of the brand Calf kept in our database (outlets per
-- platform, their items and stock, the reviews) and the handle schedule of the Aggregator team (which member works on
-- which outlet, a weekly template with exceptions per date). BotFood is a signal, not the official figure: when a status
-- differs the team checks the merchant account. Only the fields the reports need are kept: BotFood also answers with
-- proxy and GoBiz credential fields, never stored.

-- one row per outlet and platform (BotFood "branch channel"), as last read
CREATE TABLE IF NOT EXISTS integration_portal.agg_channel (
    id             bigint PRIMARY KEY,               -- BotFood branch_channel id
    bf_branch_id   bigint NOT NULL,
    name           text NOT NULL,                    -- the outlet name on the platform
    channel        text NOT NULL,                    -- GrabFood / GoFood / ShopeeFood
    rating         numeric(3, 2),
    is_open        boolean,
    items_total    integer,
    items_active   integer,
    synced_at      timestamptz,                      -- when BotFood itself last read the platform (its delay)
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- the history of every channel, one row per BotFood reading: opening on time, early close, pauses, rating trend
CREATE TABLE IF NOT EXISTS integration_portal.agg_channel_log (
    channel_id     bigint NOT NULL,
    synced_at      timestamptz NOT NULL,
    is_open        boolean,
    rating         numeric(3, 2),
    items_total    integer,
    items_active   integer,
    PRIMARY KEY (channel_id, synced_at)
);

-- every item on every channel, as last read; agg_item_event keeps each change of stock (the OOS history)
CREATE TABLE IF NOT EXISTS integration_portal.agg_item (
    id                 bigint PRIMARY KEY,           -- BotFood item id
    channel_id         bigint NOT NULL,
    name               text NOT NULL,
    category           text,
    in_stock           boolean,
    price              integer,
    last_not_active_at timestamptz,
    fetched_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_item_channel ON integration_portal.agg_item (channel_id);

CREATE TABLE IF NOT EXISTS integration_portal.agg_item_event (
    item_id    bigint NOT NULL,
    at         timestamptz NOT NULL DEFAULT now(),
    in_stock   boolean NOT NULL,
    PRIMARY KEY (item_id, at)
);

-- the reviews of the customers (GoFood, GrabFood, ShopeeFood)
CREATE TABLE IF NOT EXISTS integration_portal.agg_review (
    id                bigint PRIMARY KEY,            -- BotFood review id
    channel_id        bigint,
    bf_branch_id      bigint,
    outlet_name       text,
    channel           text,
    rating            smallint,
    comment           text,
    item_name         text,
    order_id          text,
    tags              jsonb,
    merchant_reply    text,
    merchant_reply_at timestamptz,
    is_reviewed       boolean,
    customer_name     text,
    ordered_at        timestamptz,
    created_at        timestamptz,
    fetched_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_review_created ON integration_portal.agg_review (created_at DESC);
CREATE INDEX IF NOT EXISTS agg_review_branch ON integration_portal.agg_review (bf_branch_id, created_at DESC);

-- a BotFood outlet to our outlet (ESB branch code): matched by name automatically, corrected by hand
CREATE TABLE IF NOT EXISTS integration_portal.agg_branch_map (
    bf_branch_id  bigint PRIMARY KEY,
    bf_name       text NOT NULL,
    branch_code   text,
    matched       text NOT NULL DEFAULT 'auto' CHECK (matched IN ('auto', 'manual')),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid
);

-- each pull from BotFood, logged (what, how many, the error); agg_state keeps the review backfill position
CREATE TABLE IF NOT EXISTS integration_portal.agg_pull (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,                       -- global / reviews / backfill
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    rows        integer,
    error       text
);
CREATE TABLE IF NOT EXISTS integration_portal.agg_state (
    key    text PRIMARY KEY,
    value  text
);
INSERT INTO integration_portal.agg_state (key, value) VALUES ('review_backfill_day', '2026-11-23') ON CONFLICT DO NOTHING;

-- handle schedule: the time slots of a day (the team works per slot: opening, the reports of the day …)
CREATE TABLE IF NOT EXISTS integration_portal.agg_slot (
    code     text PRIMARY KEY,
    label    text NOT NULL,
    sort     smallint NOT NULL DEFAULT 0,
    active   boolean NOT NULL DEFAULT true
);
INSERT INTO integration_portal.agg_slot (code, label, sort) VALUES ('day', 'Sepanjang hari', 0) ON CONFLICT DO NOTHING;

-- the weekly template: a member handles an outlet in a slot, on some weekdays (NULL = every day), from a date; a change
-- closes the old row (valid_to) so every past day keeps who handled it
CREATE TABLE IF NOT EXISTS integration_portal.agg_handle (
    id          bigserial PRIMARY KEY,
    branch_code text NOT NULL,
    slot        text NOT NULL REFERENCES integration_portal.agg_slot (code),
    weekdays    smallint[],                          -- ISO 1 = Monday … 7 = Sunday; NULL = every day
    user_id     uuid NOT NULL,
    valid_from  date NOT NULL,
    valid_to    date,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid
);
CREATE INDEX IF NOT EXISTS agg_handle_branch ON integration_portal.agg_handle (branch_code, slot, valid_from);

-- an exception for one date: another member, or nobody (user_id NULL), with a note
CREATE TABLE IF NOT EXISTS integration_portal.agg_handle_override (
    day          date NOT NULL,
    branch_code  text NOT NULL,
    slot         text NOT NULL REFERENCES integration_portal.agg_slot (code),
    user_id      uuid,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   uuid,
    PRIMARY KEY (day, branch_code, slot)
);
