-- 083: Calf On The Road (2026-11-09): the kinds of a driver's request (pengajuan) are a master kept by the team
-- (Master → Pengajuan) instead of a fixed list: name, a short hint for the driver, the highest amount allowed, whether
-- the receipt photo is required. The driver app shows the active kinds, in their order.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_request_kind (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    max_amount  numeric(14,2) CHECK (max_amount IS NULL OR max_amount > 0),   -- null = up to Rp 100 juta
    needs_photo boolean NOT NULL DEFAULT true,
    sort        integer NOT NULL DEFAULT 0,
    is_active   boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid
);
INSERT INTO integration_portal.cotr_request_kind (code, name, sort) VALUES
    ('fuel', 'BBM', 1), ('ice', 'Es batu', 2), ('stall', 'Lapak armada', 3), ('repair', 'Perbaikan armada', 4),
    ('parking', 'Parkir / retribusi', 5), ('other', 'Lainnya', 6)
ON CONFLICT (code) DO NOTHING;
-- the kind of a request is checked against the master now
ALTER TABLE integration_portal.cotr_request DROP CONSTRAINT IF EXISTS cotr_request_kind_check;
-- a kind may not need the receipt photo (needs_photo = false)
ALTER TABLE integration_portal.cotr_request ALTER COLUMN photo DROP NOT NULL;
ALTER TABLE integration_portal.cotr_request ALTER COLUMN photo_type DROP NOT NULL;
