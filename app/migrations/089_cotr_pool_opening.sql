-- 089: Stock of the finished products (cups per menu) of a pool / HUB (2026-11-11). Entered once: the opening stock of
-- a pool (cotr_pool_opening, per menu with its expiry); from that day on the stock runs by itself every day:
--   start of a day = what was left the day before (the opening on its day)
--   + the cups received at the pool (transfers of the batches, with the expiry of the batch)
--   − the cups given to the drivers (accepted assignments, reloads sent) + the cups they bring back at check out
--   − the waste and the cupping of the pool
--   − what expired (first expired, first out): the cups past their expiry are not carried to the next day.
-- Every lot keeps its production / batch code (cotr_batch.batch_no; on the opening as typed), so the cups left at a pool
-- are known per batch.
-- A new opening of a pool starts it again from its day (the older one stays as history).
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool_opening (
    id         bigserial PRIMARY KEY,
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day        date NOT NULL,
    menu_code  text NOT NULL REFERENCES integration_portal.cotr_menu (code) ON UPDATE CASCADE ON DELETE CASCADE,
    qty        integer NOT NULL CHECK (qty >= 0),
    expires_on date,
    batch_no   text,                                   -- the production / batch code of the cups counted
    note       text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_pool_opening_pool ON integration_portal.cotr_pool_opening (pool_code, day DESC);
