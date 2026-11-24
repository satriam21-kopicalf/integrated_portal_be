-- 144: the whole life of a Fund Request (2026-11-24, user decision): Pending → Approved → In Progress (money out, the
-- proof awaited) → Verifikasi (Finance checks the proof and the difference) → Done. "Paid" no longer ends a request whose
-- kind needs proof after the money went out (Cash in Advance, Funding Request, Marketing, Event, Maintenance, Procurement,
-- vendor payments): the requester attaches the receipts / invoices and the actual amount in the same request, Finance
-- verifies, settles the difference (money back, or the shortfall paid) and closes it. The rules of each kind are set by
-- Finance (fund_rule); a payee is a bank account or a Virtual Account (marketplaces); a vendor payment carries its due date
-- and invoice number. The status code PAID stays (money out = In Progress); VERIFYING, DONE and CANCELLED are new.

ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_status_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_status_check
    CHECK (status IN ('DRAFT', 'SUBMITTED', 'REVISION', 'APPROVED', 'PAID', 'VERIFYING', 'DONE', 'REJECTED', 'CANCELLED'));

ALTER TABLE integration_portal.pengajuan
    ADD COLUMN IF NOT EXISTS proof_due_on        date,
    ADD COLUMN IF NOT EXISTS proof_submitted_at  timestamptz,
    ADD COLUMN IF NOT EXISTS actual_amount       numeric(16, 2),     -- what the requester says was spent
    ADD COLUMN IF NOT EXISTS actual_note         text,
    ADD COLUMN IF NOT EXISTS verified_amount     numeric(16, 2),     -- what Finance accepts (adjustable, with a note)
    ADD COLUMN IF NOT EXISTS verified_note       text,
    ADD COLUMN IF NOT EXISTS verified_at         timestamptz,
    ADD COLUMN IF NOT EXISTS verified_by         text,
    ADD COLUMN IF NOT EXISTS done_at             timestamptz,
    ADD COLUMN IF NOT EXISTS topup_amount        numeric(16, 2),     -- the shortfall Finance paid on top
    ADD COLUMN IF NOT EXISTS topup_paid_at       timestamptz,
    ADD COLUMN IF NOT EXISTS payee_type          text NOT NULL DEFAULT 'BANK' CHECK (payee_type IN ('BANK', 'VA')),
    ADD COLUMN IF NOT EXISTS va_provider         text,
    ADD COLUMN IF NOT EXISTS invoice_no          text;

ALTER TABLE integration_portal.pengajuan_file DROP CONSTRAINT IF EXISTS pengajuan_file_kind_check;
ALTER TABLE integration_portal.pengajuan_file ADD CONSTRAINT pengajuan_file_kind_check
    CHECK (kind IN ('LINE', 'INCOME', 'OTHER', 'PAYMENT', 'REFUND', 'PROOF', 'TOPUP'));

-- the rules of a kind: 'PETTY_CASH' … 'BILL', 'MAINTENANCE', 'PROCUREMENT', or 'PERSONAL:<Keperluan Dana id>'
CREATE TABLE IF NOT EXISTS integration_portal.fund_rule (
    key          text PRIMARY KEY,
    proof_after  boolean NOT NULL DEFAULT false,   -- the proof comes after the money went out (In Progress until verified)
    proof_days   integer NOT NULL DEFAULT 7 CHECK (proof_days BETWEEN 1 AND 180),
    proof_docs   text[] NOT NULL DEFAULT '{}',     -- the documents asked: NOTA, INVOICE, FOTO (goods received) …
    vendor       boolean NOT NULL DEFAULT false,   -- a vendor payment: due date, invoice number and invoice file required
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text
);
CREATE TABLE IF NOT EXISTS integration_portal.fund_setting (
    key    text PRIMARY KEY,
    value  text NOT NULL
);
INSERT INTO integration_portal.fund_setting (key, value) VALUES ('block_overdue_proof', 'false'), ('min_lead_days', '3')
    ON CONFLICT DO NOTHING;

-- a Keperluan Dana for paying a vendor (asked by the user concerned)
INSERT INTO integration_portal.fund_option (list, name, sort, settings)
    VALUES ('PURPOSE', 'Vendor Payment', 35, '{"receipt": false, "outlet": false}') ON CONFLICT DO NOTHING;

INSERT INTO integration_portal.fund_rule (key, proof_after, proof_docs, vendor) VALUES
    ('MARKETING', true, '{NOTA}', false), ('EVENT', true, '{NOTA}', false), ('BILL', false, '{}', true),
    ('MAINTENANCE', true, '{INVOICE}', false), ('PROCUREMENT', true, '{INVOICE,FOTO}', false)
    ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.fund_rule (key, proof_after, proof_docs, vendor)
SELECT 'PERSONAL:' || o.id,
       coalesce(o.settings->>'type', '') = 'ADVANCE' OR (NOT coalesce((o.settings->>'receipt')::boolean, false) AND coalesce(o.settings->>'type', '') <> 'SETTLEMENT'),
       CASE WHEN o.name = 'Vendor Payment' THEN '{INVOICE,FOTO}'::text[] ELSE '{NOTA}'::text[] END,
       o.name = 'Vendor Payment'
  FROM integration_portal.fund_option o WHERE o.list = 'PURPOSE'
    ON CONFLICT DO NOTHING;
-- the settlement report is now part of the Cash in Advance itself: no new ones (the old ones stay)
UPDATE integration_portal.fund_option SET is_active = false WHERE list = 'PURPOSE' AND settings->>'type' = 'SETTLEMENT';

-- the requests already paid: closed when nothing more is awaited, else In Progress with a due date for the proof
UPDATE integration_portal.pengajuan p SET status = 'DONE', done_at = coalesce(p.paid_at, now())
 WHERE p.status = 'PAID' AND (
       -- a kind without proof after the money went out
       NOT coalesce((SELECT r.proof_after FROM integration_portal.fund_rule r
                      WHERE r.key = CASE WHEN p.kind = 'PERSONAL' THEN 'PERSONAL:' || p.purpose_id ELSE p.kind END), false)
       -- a settlement report, or a Cash in Advance whose settlement report was paid
    OR p.settles_id IS NOT NULL
    OR EXISTS (SELECT 1 FROM integration_portal.pengajuan s WHERE s.settles_id = p.id AND s.status IN ('PAID', 'DONE'))
       -- a Maintenance & Procurement fund whose request is done (settled the old way)
    OR (p.kind IN ('MAINTENANCE', 'PROCUREMENT') AND EXISTS (SELECT 1 FROM integration_portal.de_request d
                                                              WHERE d.id = p.de_request_id AND d.status = 'DONE')));
UPDATE integration_portal.pengajuan p SET proof_due_on = coalesce(p.paid_on, p.paid_at::date, current_date) + 7
 WHERE p.status = 'PAID' AND p.proof_due_on IS NULL;
