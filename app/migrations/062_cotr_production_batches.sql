-- 062 (2026-11-03): production at the main hub and its distribution to the pools, with the production and expiry date
-- of every batch. A batch is made at a production hub (cotr_pool.is_production) for one menu: its quantity, produced
-- on, expires on (from the shelf life of the menu by default). It is sent to the pools in parts (cotr_transfer), the
-- storage of the pool marks a transfer received. The stock of a pool per batch (first expired, first out) is what it
-- received; the storage sees the expiry of what it hands to the drivers.
ALTER TABLE integration_portal.cotr_pool ADD COLUMN IF NOT EXISTS is_production boolean NOT NULL DEFAULT false;
ALTER TABLE integration_portal.cotr_menu ADD COLUMN IF NOT EXISTS shelf_life_days integer CHECK (shelf_life_days > 0);

CREATE SEQUENCE IF NOT EXISTS integration_portal.cotr_batch_no;
CREATE TABLE IF NOT EXISTS integration_portal.cotr_batch (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_no    text        NOT NULL UNIQUE,
    hub_code    text        NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    menu_code   text        NOT NULL REFERENCES integration_portal.cotr_menu (code) ON UPDATE CASCADE,
    qty         integer     NOT NULL CHECK (qty > 0),
    produced_on date        NOT NULL,
    expires_on  date        NOT NULL,
    note        text,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_on >= produced_on)
);
CREATE INDEX IF NOT EXISTS cotr_batch_day ON integration_portal.cotr_batch (produced_on DESC);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_transfer (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id    uuid        NOT NULL REFERENCES integration_portal.cotr_batch (id) ON DELETE CASCADE,
    pool_code   text        NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    qty         integer     NOT NULL CHECK (qty > 0),
    status      text        NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'received')),
    note        text,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    sent_by     uuid,
    received_at timestamptz,
    received_by uuid
);
CREATE INDEX IF NOT EXISTS cotr_transfer_pool ON integration_portal.cotr_transfer (pool_code, status);
