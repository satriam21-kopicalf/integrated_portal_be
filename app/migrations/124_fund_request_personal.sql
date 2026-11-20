-- 124: Fund Request Personal (2026-11-20): besides the outlet requests, a person asks for money for themselves or their
-- department (Deposit / Cash in Advance, Reimbursement, Pengajuan Dana, Laporan Penyelesaian CA, Refund, Petty Cash
-- departemen …), as the Google Form did. The lists of the form (Keperluan Dana, Jenis Transaksi, Project, Jenis
-- Rekening) are set by management in Monitoring Fund Request → Pengaturan; every Keperluan Dana has its own approval
-- flow (approval_step kind 'PERSONAL:<option id>', else the default 'PERSONAL'). A personal request has no outlet unless
-- one is chosen; a Laporan Penyelesaian points at the Cash in Advance it settles.

ALTER TABLE integration_portal.pengajuan ALTER COLUMN branch_code DROP NOT NULL;
ALTER TABLE integration_portal.pengajuan ALTER COLUMN branch_name DROP NOT NULL;
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'integration_portal.pengajuan'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) ILIKE '%kind%'
    LOOP
        EXECUTE format('ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE integration_portal.pengajuan
    ADD CONSTRAINT pengajuan_kind_check CHECK (kind IN ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL', 'PERSONAL'));

CREATE TABLE IF NOT EXISTS integration_portal.fund_option (
    id          serial      PRIMARY KEY,
    list        text        NOT NULL CHECK (list IN ('PURPOSE', 'TRANSACTION', 'PROJECT', 'ACCOUNT')),
    name        text        NOT NULL,
    is_active   boolean     NOT NULL DEFAULT true,
    sort        integer     NOT NULL DEFAULT 0,
    -- PURPOSE: {"receipt": bool, "outlet": bool, "type": "ADVANCE" | "SETTLEMENT" | null, "deptPlafon": bool}
    settings    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
CREATE UNIQUE INDEX IF NOT EXISTS fund_option_name_uniq ON integration_portal.fund_option (list, lower(name));

INSERT INTO integration_portal.fund_option (list, name, sort, settings) VALUES
    ('PURPOSE', 'Deposit / Cash in Advance', 10, '{"receipt": false, "outlet": false, "type": "ADVANCE"}'),
    ('PURPOSE', 'Reimbursement', 20, '{"receipt": true, "outlet": false}'),
    ('PURPOSE', 'Pengajuan Dana', 30, '{"receipt": false, "outlet": false}'),
    ('PURPOSE', 'Laporan Penyelesaian Deposit / Cash in Advance', 40, '{"receipt": true, "outlet": false, "type": "SETTLEMENT"}'),
    ('PURPOSE', 'Refund', 50, '{"receipt": true, "outlet": false}'),
    ('PURPOSE', 'Petty Cash Departemen', 60, '{"receipt": true, "outlet": false, "deptPlafon": true}'),
    ('ACCOUNT', 'Rekening Pribadi', 10, '{}'),
    ('ACCOUNT', 'Bank Transfer', 20, '{}'),
    ('ACCOUNT', 'Virtual Account (VA)', 30, '{}'),
    ('ACCOUNT', 'Credit Card', 40, '{}'),
    ('ACCOUNT', 'Debit Card', 50, '{}'),
    ('ACCOUNT', 'Lainnya', 90, '{}')
ON CONFLICT DO NOTHING;

ALTER TABLE integration_portal.pengajuan
    ADD COLUMN IF NOT EXISTS email               text,
    ADD COLUMN IF NOT EXISTS department          text,
    ADD COLUMN IF NOT EXISTS job_title           text,
    ADD COLUMN IF NOT EXISTS purpose_id          integer REFERENCES integration_portal.fund_option (id),
    ADD COLUMN IF NOT EXISTS transaction_type_id integer REFERENCES integration_portal.fund_option (id),
    ADD COLUMN IF NOT EXISTS project_id          integer REFERENCES integration_portal.fund_option (id),
    ADD COLUMN IF NOT EXISTS account_type_id     integer REFERENCES integration_portal.fund_option (id),
    ADD COLUMN IF NOT EXISTS settles_id          uuid REFERENCES integration_portal.pengajuan (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS pengajuan_settles_idx ON integration_portal.pengajuan (settles_id) WHERE settles_id IS NOT NULL;

-- the most a department may ask at once for a Keperluan Dana marked deptPlafon (none set = no limit)
CREATE TABLE IF NOT EXISTS integration_portal.fund_dept_plafon (
    department  text        PRIMARY KEY,
    amount      numeric     NOT NULL CHECK (amount > 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);

-- the default flow of a personal request (a Keperluan Dana without its own): management approves, Finance pays
INSERT INTO integration_portal.approval_step (form, kind, position, label, step_type)
SELECT 'pengajuan', 'PERSONAL', 1, 'Approval Management', 'APPROVE'
 WHERE NOT EXISTS (SELECT 1 FROM integration_portal.approval_step WHERE form = 'pengajuan' AND kind = 'PERSONAL');
INSERT INTO integration_portal.approval_step (form, kind, position, label, step_type)
SELECT 'pengajuan', 'PERSONAL', 2, 'Pembayaran Finance', 'PAY'
 WHERE NOT EXISTS (SELECT 1 FROM integration_portal.approval_step WHERE form = 'pengajuan' AND kind = 'PERSONAL' AND step_type = 'PAY');
