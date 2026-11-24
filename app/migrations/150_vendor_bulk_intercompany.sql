-- 150: vendor payments in bulk and the money between the PTs (2026-11-25, app/fund_vendor.py, app/pengajuan.py).
-- fund_external: invoices already paid outside (the Google Form recap), refused when asked again here.
CREATE TABLE IF NOT EXISTS integration_portal.fund_external (
    id         bigserial PRIMARY KEY,
    account    text NOT NULL,
    invoice    text NOT NULL,
    amount     numeric(16, 2) NOT NULL,
    label      text NOT NULL,
    vendor     text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account, invoice, amount)
);
-- INTERCOMPANY (Antar-PT): not a cost. One per unit, place and period; done once the receiving PT confirms the money.
ALTER TABLE integration_portal.pengajuan
    ADD COLUMN IF NOT EXISTS payee_pt text,
    ADD COLUMN IF NOT EXISTS received_at timestamptz,
    ADD COLUMN IF NOT EXISTS received_by text,
    ADD COLUMN IF NOT EXISTS received_note text;
ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_kind_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_kind_check CHECK (kind IN
    ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL', 'PERSONAL', 'MAINTENANCE', 'PROCUREMENT', 'VENDOR', 'INTERCOMPANY'));
-- who confirms the money a PT received (Configurations; none = superadmins)
CREATE TABLE IF NOT EXISTS integration_portal.fund_receiver (
    payer   text NOT NULL,
    user_id uuid NOT NULL REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    PRIMARY KEY (payer, user_id)
);
INSERT INTO integration_portal.approval_step (form, kind, position, label, step_type) VALUES
    ('pengajuan', 'INTERCOMPANY', 1, 'Approval Head Division', 'APPROVE'), ('pengajuan', 'INTERCOMPANY', 2, 'Pembayaran Finance', 'PAY')
ON CONFLICT (form, kind, position) DO NOTHING;
