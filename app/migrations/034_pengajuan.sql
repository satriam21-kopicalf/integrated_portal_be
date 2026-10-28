-- 034: Form & Monitoring Pengajuan (2026-10-28, app/pengajuan.py). An outlet reports its petty cash for a period and asks
-- for the money spent: branch, baristas (accounts of the branch + names typed), opening balance, money in (with its
-- transfer proof), the expenses one by one (each with its receipt / invoice) and the closing balance. Operations verifies
-- the evidence, Finance approves the amount and records the payment (transfer proof); a step may send it back for a
-- revision or reject it (with a note). Files on the backend volume (MEDIA_DIR/pengajuan), not in the database.
-- Pages: form.pengajuan (the outlet accounts), mon.pengajuan (monitoring), mon.pengajuan.verify, mon.pengajuan.finance.

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    number            text          UNIQUE,                      -- PGJ-YYMM-NNNN, given at the first sending
    status            text          NOT NULL DEFAULT 'DRAFT' CHECK (status IN
                      ('DRAFT', 'SUBMITTED', 'REVISION', 'VERIFIED', 'APPROVED', 'PAID', 'REJECTED')),
    user_id           uuid          NOT NULL,
    username          text          NOT NULL,
    full_name         text,
    branch_code       text          NOT NULL,
    branch_name       text          NOT NULL,
    baristas          jsonb         NOT NULL DEFAULT '[]',       -- [{"name", "userId"?}]
    period_from       date,
    period_to         date,
    opening_balance   numeric(16, 2),
    opening_expected  numeric(16, 2),                            -- closing balance of the previous pengajuan of the branch
    opening_note      text,                                      -- why the opening balance differs from it
    income_amount     numeric(16, 2) NOT NULL DEFAULT 0,
    income_note       text,
    total_out         numeric(16, 2) NOT NULL DEFAULT 0,
    closing_balance   numeric(16, 2),
    note              text,
    approved_amount   numeric(16, 2),                            -- expenses accepted by Finance
    paid_amount       numeric(16, 2),
    paid_on           date,
    paid_note         text,
    created_at        timestamptz   NOT NULL DEFAULT now(),
    updated_at        timestamptz   NOT NULL DEFAULT now(),
    submitted_at      timestamptz,                               -- the latest sending
    first_submitted_at timestamptz,
    verified_at       timestamptz,
    verified_by       text,
    approved_at       timestamptz,
    approved_by       text,
    paid_at           timestamptz,
    paid_by           text,
    closed_at         timestamptz                                -- PAID or REJECTED
);
CREATE INDEX IF NOT EXISTS pengajuan_branch_idx ON integration_portal.pengajuan (branch_code, submitted_at);
CREATE INDEX IF NOT EXISTS pengajuan_user_idx ON integration_portal.pengajuan (user_id, status);

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_line (
    id            serial        PRIMARY KEY,
    pengajuan_id  uuid          NOT NULL REFERENCES integration_portal.pengajuan (id) ON DELETE CASCADE,
    sort          integer       NOT NULL DEFAULT 0,
    spent_on      date,
    category      text          NOT NULL DEFAULT 'OTHER',
    description   text          NOT NULL DEFAULT '',
    amount        numeric(16, 2) NOT NULL DEFAULT 0,
    review        text          NOT NULL DEFAULT 'OK' CHECK (review IN ('OK', 'REJECTED')),
    review_note   text,
    reviewed_by   text
);
CREATE INDEX IF NOT EXISTS pengajuan_line_idx ON integration_portal.pengajuan_line (pengajuan_id, sort);

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_file (
    id            uuid          PRIMARY KEY,
    pengajuan_id  uuid          NOT NULL REFERENCES integration_portal.pengajuan (id) ON DELETE CASCADE,
    line_id       integer       REFERENCES integration_portal.pengajuan_line (id) ON DELETE CASCADE,
    kind          text          NOT NULL CHECK (kind IN ('LINE', 'INCOME', 'OTHER', 'PAYMENT')),
    doc_type      text          NOT NULL DEFAULT 'NOTA',
    storage_path  text          NOT NULL,
    mime          text          NOT NULL,
    bytes         integer       NOT NULL,
    sha256        text          NOT NULL,
    file_name     text,
    uploaded_by   uuid,
    uploaded_by_name text,
    uploaded_at   timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pengajuan_file_idx ON integration_portal.pengajuan_file (pengajuan_id);
CREATE INDEX IF NOT EXISTS pengajuan_file_sha_idx ON integration_portal.pengajuan_file (sha256);

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_event (
    id            bigserial     PRIMARY KEY,
    pengajuan_id  uuid          NOT NULL REFERENCES integration_portal.pengajuan (id) ON DELETE CASCADE,
    at            timestamptz   NOT NULL DEFAULT now(),
    actor_id      uuid,
    actor_name    text,
    kind          text          NOT NULL,                        -- SUBMIT | STATUS | LINE | NOTE
    from_status   text,
    to_status     text,
    note          text,
    details       jsonb         NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS pengajuan_event_idx ON integration_portal.pengajuan_event (pengajuan_id, at);

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_counter (
    month  text     PRIMARY KEY,
    n      integer  NOT NULL
);

-- the outlet accounts fill the form (outlet.*, the Wasgee Tea outlets): an exception on top of role "user"
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT id, 'form.pengajuan', true FROM integration_portal.user_account
    WHERE role = 'user' AND split_part(username, '.', 1) IN ('outlet', 'wasgee')
ON CONFLICT (user_id, page) DO UPDATE SET allowed = true;
