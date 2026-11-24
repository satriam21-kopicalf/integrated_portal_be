-- 151: Aggregator Insight stage 3 (2026-11-25, app/aggregator_quality.py): complaint tickets, the reason of every void and the
-- voids entered by hand, the price changes of the delivery menus, and the opening hours the team enters for the outlets
-- BotFood has none for.
CREATE TABLE IF NOT EXISTS integration_portal.agg_complaint (
    id                bigserial PRIMARY KEY,
    review_id         bigint UNIQUE,
    branch_code       text,
    channel           text NOT NULL,
    order_no          text,
    happened_on       date NOT NULL,
    source            text NOT NULL CHECK (source IN ('review', 'chat', 'app', 'other')),
    category          text NOT NULL,
    detail            text NOT NULL,
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'progress', 'done')),
    resolution        text,
    compensation      numeric(14, 2),
    handled_by_name   text,
    created_by        uuid,
    created_by_name   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    first_response_at timestamptz,
    resolved_at       timestamptz
);
CREATE INDEX IF NOT EXISTS agg_complaint_day ON integration_portal.agg_complaint (happened_on);

CREATE TABLE IF NOT EXISTS integration_portal.agg_void (
    id          bigserial PRIMARY KEY,
    sales_num   text UNIQUE,                  -- the ESB bill; none = entered by hand
    branch_code text,
    channel     text,
    order_no    text,
    happened_on date,
    amount      numeric(14, 2),
    reason      text NOT NULL,
    note        text,
    by_name     text,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_void_day ON integration_portal.agg_void (happened_on) WHERE sales_num IS NULL;

CREATE TABLE IF NOT EXISTS integration_portal.agg_hours (
    branch_code text PRIMARY KEY,
    open_time   time NOT NULL,
    close_time  time,
    by_name     text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration_portal.agg_price_log (
    id        bigserial PRIMARY KEY,
    item_id   bigint NOT NULL,
    old_price numeric,
    new_price numeric,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_price_log_at ON integration_portal.agg_price_log (at);
CREATE OR REPLACE FUNCTION integration_portal.agg_price_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO integration_portal.agg_price_log (item_id, old_price, new_price) VALUES (NEW.id, OLD.price, NEW.price);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS agg_item_price ON integration_portal.agg_item;
CREATE TRIGGER agg_item_price AFTER UPDATE OF price ON integration_portal.agg_item FOR EACH ROW
    WHEN (OLD.price IS NOT NULL AND NEW.price IS NOT NULL AND OLD.price IS DISTINCT FROM NEW.price)
    EXECUTE FUNCTION integration_portal.agg_price_changed();

-- whoever already opens Out of Stock opens the new modules; narrowed in User Accounts
INSERT INTO integration_portal.role_page (role, page)
    SELECT r.role, p.page FROM integration_portal.role_page r, (VALUES ('agg.reviews'), ('agg.complaints'), ('agg.void'), ('agg.menu'), ('agg.reports')) AS p (page)
    WHERE r.page = 'agg.oos' ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT u.user_id, p.page, u.allowed FROM integration_portal.user_page u, (VALUES ('agg.reviews'), ('agg.complaints'), ('agg.void'), ('agg.menu'), ('agg.reports')) AS p (page)
    WHERE u.page = 'agg.oos' ON CONFLICT DO NOTHING;
