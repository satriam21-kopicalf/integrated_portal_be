-- 063 (2026-11-03): the full record of an armada and its information label. The vehicle papers and upkeep (asset
-- number, colour, year, battery, STNK, insurance, odometer, next service, GPS tracker) complete Master Fleet; the QR
-- of the printed label opens /share/fleet/{label_token}: the armada and its driver(s), no sign-in, without the papers,
-- the QRIS or the phone numbers (signed-in COTR users see everything). A new token voids the labels printed before.
ALTER TABLE integration_portal.cotr_fleet
    ADD COLUMN IF NOT EXISTS asset_no          text,
    ADD COLUMN IF NOT EXISTS color             text,
    ADD COLUMN IF NOT EXISTS year_made         integer CHECK (year_made BETWEEN 1980 AND 2100),
    ADD COLUMN IF NOT EXISTS battery_no        text,
    ADD COLUMN IF NOT EXISTS stnk_no           text,
    ADD COLUMN IF NOT EXISTS stnk_expires      date,
    ADD COLUMN IF NOT EXISTS insurance_no      text,
    ADD COLUMN IF NOT EXISTS insurance_expires date,
    ADD COLUMN IF NOT EXISTS odometer_km       integer CHECK (odometer_km >= 0),
    ADD COLUMN IF NOT EXISTS next_service      date,
    ADD COLUMN IF NOT EXISTS gps_id            text,
    ADD COLUMN IF NOT EXISTS label_token       text NOT NULL DEFAULT substr(replace(gen_random_uuid()::text, '-', ''), 1, 20);
CREATE UNIQUE INDEX IF NOT EXISTS cotr_fleet_label_token ON integration_portal.cotr_fleet (label_token);
