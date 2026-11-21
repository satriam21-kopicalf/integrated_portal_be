-- 130: Maintenance & Procurement requests funded through Fund Request (2026-11-21, app/de_funds.py). The team asks
-- Finance for the money of a Damaged Equipment request from its cost lines: a pengajuan of kind MAINTENANCE or
-- PROCUREMENT linked to the request. Its work waits for the approval; at Done the difference with the actual cost is
-- settled (a top-up request, or a refund Finance confirms). Fund Request is the only way the money goes out.

ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_kind_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_kind_check
    CHECK (kind IN ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL', 'PERSONAL', 'MAINTENANCE', 'PROCUREMENT'));

ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS de_request_id uuid REFERENCES integration_portal.de_request (id);
ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS refund_amount numeric(16, 2);   -- spent less than paid
ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS refunded_at timestamptz;          -- Finance got it back
ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS refunded_by text;
ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS refunded_note text;

-- one fund per request at a time (the top-up of the settlement has settles_id and is apart)
CREATE UNIQUE INDEX IF NOT EXISTS pengajuan_de_fund_uniq ON integration_portal.pengajuan (de_request_id)
    WHERE de_request_id IS NOT NULL AND settles_id IS NULL AND status <> 'REJECTED';
CREATE INDEX IF NOT EXISTS pengajuan_de_request ON integration_portal.pengajuan (de_request_id) WHERE de_request_id IS NOT NULL;

ALTER TABLE integration_portal.pengajuan_file DROP CONSTRAINT IF EXISTS pengajuan_file_kind_check;
ALTER TABLE integration_portal.pengajuan_file ADD CONSTRAINT pengajuan_file_kind_check
    CHECK (kind IN ('LINE', 'INCOME', 'OTHER', 'PAYMENT', 'REFUND'));

-- the request's timeline shows every step of its fund
ALTER TABLE integration_portal.de_event DROP CONSTRAINT IF EXISTS de_event_kind_check;
ALTER TABLE integration_portal.de_event ADD CONSTRAINT de_event_kind_check
    CHECK (kind IN ('SUBMIT', 'STATUS', 'ASSIGN', 'RELEASE', 'SWAP', 'COST', 'NOTE', 'DUPLICATE', 'WORK', 'TARGET', 'HANDOVER', 'FUND'));
