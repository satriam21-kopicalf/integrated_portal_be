-- 037: Form Fund (Pengajuan) after the MoM of 2026-10-27 (2026-10-29, user request; app/pengajuan.py).
--   * kinds: PETTY_CASH (weekly, request based: the report of the week and the top-up up to the plafon; no request, no
--     transfer) and the budget requests EMERGENCY (urgent purchase already paid), MARKETING, EVENT, BILL (electricity,
--     water, internet …). Maintenance and assets stay in Damaged Equipment.
--   * the fields of the operational team's form: submitted by (barista / PIC), personal phone, bank account and its
--     holder; a budget request also has its purpose and the date needed.
--   * approvals set by management, per kind: any number of approval steps in order, each with its users (existing
--     accounts), then the payment step (Finance). No amount threshold. A step without users: superadmins only.
--     Replaces the pages mon.pengajuan.verify / mon.pengajuan.finance (nobody had them).
--   * plafon of the petty cash: one for every outlet ('*') and per outlet.
--   * role outlet (outlet accounts) fills the form; the HUB CK / HUB WH accounts not (an exception per account).
-- Only one draft existed: the status VERIFIED is dropped.

DELETE FROM integration_portal.pengajuan WHERE status = 'VERIFIED';
ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_status_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_status_check
    CHECK (status IN ('DRAFT', 'SUBMITTED', 'REVISION', 'APPROVED', 'PAID', 'REJECTED'));
ALTER TABLE integration_portal.pengajuan
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'PETTY_CASH'
        CHECK (kind IN ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL')),
    ADD COLUMN IF NOT EXISTS approval_step integer NOT NULL DEFAULT 0,      -- SUBMITTED: the approval step waiting (1 …)
    ADD COLUMN IF NOT EXISTS requester_name text,
    ADD COLUMN IF NOT EXISTS requester_phone text,
    ADD COLUMN IF NOT EXISTS payee_bank text,
    ADD COLUMN IF NOT EXISTS payee_account text,
    ADD COLUMN IF NOT EXISTS payee_name text,
    ADD COLUMN IF NOT EXISTS purpose text,
    ADD COLUMN IF NOT EXISTS needed_on date,
    ADD COLUMN IF NOT EXISTS plafon numeric(16, 2),                         -- petty cash: the plafon when sent
    ADD COLUMN IF NOT EXISTS requested_amount numeric(16, 2);               -- what the outlet asks to receive
CREATE INDEX IF NOT EXISTS pengajuan_kind_idx ON integration_portal.pengajuan (kind, status);

CREATE TABLE IF NOT EXISTS integration_portal.approval_step (
    id          serial       PRIMARY KEY,
    form        text         NOT NULL,                     -- pengajuan
    kind        text         NOT NULL,
    position    integer      NOT NULL,
    label       text         NOT NULL,
    step_type   text         NOT NULL DEFAULT 'APPROVE' CHECK (step_type IN ('APPROVE', 'PAY')),
    UNIQUE (form, kind, position)
);
CREATE TABLE IF NOT EXISTS integration_portal.approval_step_user (
    step_id     integer      NOT NULL REFERENCES integration_portal.approval_step (id) ON DELETE CASCADE,
    user_id     uuid         NOT NULL REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    PRIMARY KEY (step_id, user_id)
);
INSERT INTO integration_portal.approval_step (form, kind, position, label, step_type)
    SELECT 'pengajuan', k, s.position, s.label, s.step_type
    FROM unnest(ARRAY['PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT', 'BILL']) AS k,
         (VALUES (1, 'Approval Management', 'APPROVE'), (2, 'Pembayaran Finance', 'PAY')) AS s (position, label, step_type)
ON CONFLICT (form, kind, position) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_plafon (
    branch_code text         PRIMARY KEY,                  -- '*' = every outlet without its own plafon
    amount      numeric(16, 2) NOT NULL CHECK (amount >= 0),
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    updated_by  text
);

DELETE FROM integration_portal.user_page WHERE page IN ('mon.pengajuan.verify', 'mon.pengajuan.finance');
DELETE FROM integration_portal.role_page WHERE page IN ('mon.pengajuan.verify', 'mon.pengajuan.finance');

INSERT INTO integration_portal.role_page (role, page) VALUES ('outlet', 'form.pengajuan') ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT id, 'form.pengajuan', false FROM integration_portal.user_account
    WHERE role = 'outlet' AND (split_part(username, '.', 1) IN ('hubck', 'hubwh', 'wh')
                               OR username IN ('ck.pusat', 'roastery.pusat') OR job_title IN ('HUB WH', 'HUB CK'))
ON CONFLICT (user_id, page) DO UPDATE SET allowed = false;

-- Damaged Equipment → Procurement: merchandise (the items of the menu category MERCH in ESB, the sizes of MERCH SIZE)
INSERT INTO integration_portal.de_category (grp, name, default_urgency, examples, sort)
    SELECT 'Merchandise', 'Merchandise', 'PLANNED', ARRAY['T-shirt', 'Tumbler', 'Totebag', 'Notebook', 'Pulpen'], 70
    WHERE NOT EXISTS (SELECT 1 FROM integration_portal.de_category WHERE grp = 'Merchandise');
