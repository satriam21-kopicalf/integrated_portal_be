-- 149: the physical leftover of a COTR pool revised afterwards (2026-11-24): the old and the new cups per menu, why and by whom.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool_revision (
    id         bigserial PRIMARY KEY,
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day        date NOT NULL,
    menu_code  text NOT NULL,
    old_qty    integer NOT NULL,
    new_qty    integer NOT NULL,
    reason     text,
    by_name    text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_pool_revision_day ON integration_portal.cotr_pool_revision (pool_code, day);
