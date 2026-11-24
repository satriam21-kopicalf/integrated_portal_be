-- 148: Fund Request per unit (2026-11-24, app/fund_units.py): Roastery, Warehouse and Central Kitchen beside Kopi Calf,
-- Wasgee and Calf On The Road, the PT that pays each, the units of an account, and the kind VENDOR (a vendor's invoice:
-- term COD / TOP / CBD, invoice number and date, due date, outstanding; the recap of the Warehouse).
CREATE TABLE IF NOT EXISTS integration_portal.fund_unit (
    code    text PRIMARY KEY,
    name    text NOT NULL,
    payer   text NOT NULL,
    vendor  boolean NOT NULL DEFAULT false,   -- the form offers the vendor payment
    sort    integer NOT NULL DEFAULT 0
);
INSERT INTO integration_portal.fund_unit (code, name, payer, vendor, sort) VALUES
    ('CALF', 'Kopi Calf', 'PT YUDA PRAWIRA GROUP', false, 10),
    ('CALF7', 'Wasgee Tea', 'PT YUDA PRAWIRA GROUP', false, 20),
    ('COTR', 'Calf On The Road', 'PT YUDA PRAWIRA GROUP', false, 30),
    ('ROASTERY', 'Roastery', 'PT DAYUPRA SOLUSI PARTNER', true, 40),
    ('WAREHOUSE', 'Warehouse', 'PT DAYUPRA SOLUSI PARTNER', true, 50),
    ('CK', 'Central Kitchen', 'PT DAYUPRA SOLUSI PARTNER', true, 60)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.user_fund_unit (
    user_id uuid NOT NULL REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    unit    text NOT NULL REFERENCES integration_portal.fund_unit (code),
    PRIMARY KEY (user_id, unit)
);

ALTER TABLE integration_portal.pengajuan
    ADD COLUMN IF NOT EXISTS unit text REFERENCES integration_portal.fund_unit (code),
    ADD COLUMN IF NOT EXISTS payer text,
    ADD COLUMN IF NOT EXISTS vendor_name text,
    ADD COLUMN IF NOT EXISTS payment_term text CHECK (payment_term IN ('COD', 'TOP', 'CBD')),
    ADD COLUMN IF NOT EXISTS invoice_date date,
    ADD COLUMN IF NOT EXISTS outstanding_amount numeric(16, 2);
CREATE INDEX IF NOT EXISTS pengajuan_unit_idx ON integration_portal.pengajuan (unit, status);
CREATE INDEX IF NOT EXISTS pengajuan_account_idx ON integration_portal.pengajuan (payee_account, requested_amount);

ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_kind_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_kind_check
    CHECK (kind IN ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL', 'PERSONAL', 'MAINTENANCE', 'PROCUREMENT', 'VENDOR'));

-- the requests so far: the unit of their place (their payer stays what it was: the Kopi Calf account)
UPDATE integration_portal.pengajuan SET unit = 'CALF', payer = 'PT YUDA PRAWIRA GROUP' WHERE unit IS NULL;

-- the vendor payment: head of division, then Finance (the users are chosen in Configurations; until then superadmins)
INSERT INTO integration_portal.approval_step (form, kind, position, label, step_type) VALUES
    ('pengajuan', 'VENDOR', 1, 'Approval Head Division', 'APPROVE'), ('pengajuan', 'VENDOR', 2, 'Pembayaran Finance', 'PAY')
ON CONFLICT (form, kind, position) DO NOTHING;
INSERT INTO integration_portal.approval_step_user (step_id, user_id)
    SELECT v.id, su.user_id FROM integration_portal.approval_step v
    JOIN integration_portal.approval_step b ON b.form = 'pengajuan' AND b.kind = 'BILL' AND b.step_type = 'PAY'
    JOIN integration_portal.approval_step_user su ON su.step_id = b.id
    WHERE v.form = 'pengajuan' AND v.kind = 'VENDOR' AND v.step_type = 'PAY'
ON CONFLICT DO NOTHING;
