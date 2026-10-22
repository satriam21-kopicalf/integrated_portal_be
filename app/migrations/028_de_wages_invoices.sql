-- 028: Damaged Equipment (2026-10-22): the pay of a technician (daily or monthly, adjustable) and the invoices /
-- receipts of the cost attached while a request is handled (PDF or photo, kept with the other media of the request).

ALTER TABLE integration_portal.de_technician
    ADD COLUMN IF NOT EXISTS wage_type   text          NOT NULL DEFAULT 'DAILY' CHECK (wage_type IN ('DAILY', 'MONTHLY')),
    ADD COLUMN IF NOT EXISTS wage_amount numeric(14, 2) CHECK (wage_amount IS NULL OR wage_amount >= 0);

ALTER TABLE integration_portal.de_media DROP CONSTRAINT IF EXISTS de_media_kind_check;
ALTER TABLE integration_portal.de_media ADD CONSTRAINT de_media_kind_check CHECK (kind IN ('PHOTO', 'VIDEO', 'AFTER', 'INVOICE'));
ALTER TABLE integration_portal.de_media ADD COLUMN IF NOT EXISTS note text;  -- e.g. invoice number / what it pays
