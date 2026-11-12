-- 094: the night closing of a pool / HUB (recommendation #15, 2026-11-12): 1) waste & cupping of the pool, 2) the physical
-- leftover per batch code, 3) the balance checked (a note when it is not 0), then the day is closed. The figures of that
-- moment are kept; a closed day can be opened again (who and when stay in the activity log).
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool_close (
    pool_code  text NOT NULL,
    day        date NOT NULL,
    note       text,
    snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,   -- per menu: calculated, actual, balance, waste, cupping, sold
    closed_by  uuid,
    closed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pool_code, day)
);
