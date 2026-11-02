-- 050 (2026-11-02): complete masters of Calf On The Road.
-- Driver: photo, person (NIK, birth, gender, address, emergency contact), employment (status, position, contract),
-- payroll (fixed monthly salary, allowance) and bank account. Salary, bank and NIK are read and written only by
-- superadmins and the COTR management (page cotr.management, app/cotr.py SENSITIVE).
-- Fleet: name, type from a master (electric bike, bike, motorbike, cart, booth, car …), make / model, frame number,
-- acquisition date, photo and the QRIS of the armada (bank, NMID, merchant name, QR image); printed as a label.
-- Pool / HUB: coordinates and check-in radius, PIC, phone, opening hours.
ALTER TABLE integration_portal.cotr_driver
    ADD COLUMN IF NOT EXISTS photo bytea,
    ADD COLUMN IF NOT EXISTS photo_type text,
    ADD COLUMN IF NOT EXISTS nik text,
    ADD COLUMN IF NOT EXISTS birth_place text,
    ADD COLUMN IF NOT EXISTS birth_date date,
    ADD COLUMN IF NOT EXISTS gender text CHECK (gender IN ('male', 'female')),
    ADD COLUMN IF NOT EXISTS address text,
    ADD COLUMN IF NOT EXISTS emergency_name text,
    ADD COLUMN IF NOT EXISTS emergency_phone text,
    ADD COLUMN IF NOT EXISTS employment_status text CHECK (employment_status IN ('permanent', 'contract', 'daily', 'intern')),
    ADD COLUMN IF NOT EXISTS position text,
    ADD COLUMN IF NOT EXISTS contract_end date,
    ADD COLUMN IF NOT EXISTS salary_monthly numeric(14,2) CHECK (salary_monthly >= 0),
    ADD COLUMN IF NOT EXISTS allowance_monthly numeric(14,2) CHECK (allowance_monthly >= 0),
    ADD COLUMN IF NOT EXISTS bank_name text,
    ADD COLUMN IF NOT EXISTS bank_account text,
    ADD COLUMN IF NOT EXISTS bank_holder text,
    ADD COLUMN IF NOT EXISTS note text;

CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet_type (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    sort       integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);
INSERT INTO integration_portal.cotr_fleet_type (code, name, sort) VALUES
    ('sepeda_listrik', 'Sepeda listrik', 1), ('sepeda', 'Sepeda', 2), ('motor', 'Motor', 3),
    ('gerobak', 'Gerobak', 4), ('booth', 'Booth', 5), ('mobil', 'Mobil', 6)
ON CONFLICT (code) DO NOTHING;
ALTER TABLE integration_portal.cotr_fleet DROP CONSTRAINT IF EXISTS cotr_fleet_type_check;
ALTER TABLE integration_portal.cotr_fleet DROP CONSTRAINT IF EXISTS cotr_fleet_type_fk;
ALTER TABLE integration_portal.cotr_fleet
    ADD CONSTRAINT cotr_fleet_type_fk FOREIGN KEY (type) REFERENCES integration_portal.cotr_fleet_type (code) ON UPDATE CASCADE;
ALTER TABLE integration_portal.cotr_fleet
    ADD COLUMN IF NOT EXISTS name text,
    ADD COLUMN IF NOT EXISTS brand text,
    ADD COLUMN IF NOT EXISTS model text,
    ADD COLUMN IF NOT EXISTS frame_no text,
    ADD COLUMN IF NOT EXISTS acquired_on date,
    ADD COLUMN IF NOT EXISTS photo bytea,
    ADD COLUMN IF NOT EXISTS photo_type text,
    ADD COLUMN IF NOT EXISTS qris_bank text,
    ADD COLUMN IF NOT EXISTS qris_nmid text,
    ADD COLUMN IF NOT EXISTS qris_merchant text,
    ADD COLUMN IF NOT EXISTS qris_image bytea,
    ADD COLUMN IF NOT EXISTS qris_image_type text,
    ADD COLUMN IF NOT EXISTS note text;
-- the armada of 047 are named after their QRIS account (BNI 01 …)
UPDATE integration_portal.cotr_fleet SET name = 'Armada ' || code,
       qris_bank = split_part(code, ' ', 1)
 WHERE name IS NULL AND code ~ '^(BNI|BRI) [0-9]+$';

ALTER TABLE integration_portal.cotr_pool
    ADD COLUMN IF NOT EXISTS latitude numeric(9,6),
    ADD COLUMN IF NOT EXISTS longitude numeric(9,6),
    ADD COLUMN IF NOT EXISTS radius_m integer NOT NULL DEFAULT 100 CHECK (radius_m BETWEEN 10 AND 5000),
    ADD COLUMN IF NOT EXISTS pic_name text,
    ADD COLUMN IF NOT EXISTS phone text,
    ADD COLUMN IF NOT EXISTS open_time time,
    ADD COLUMN IF NOT EXISTS close_time time,
    ADD COLUMN IF NOT EXISTS note text;
