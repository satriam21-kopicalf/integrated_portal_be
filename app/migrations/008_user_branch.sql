-- 008: branches a user may see.
--
-- Role "user" only sees the Overview and Sales Transactions of the branches listed
-- here (enforced by the backend on every data endpoint: overview, transactions,
-- summary, live, exports, branch list). A user without branches sees no data.
-- Superadmins always see every branch; rows for them are ignored.

CREATE TABLE IF NOT EXISTS integration_portal.user_branch (
    user_id     uuid        NOT NULL REFERENCES integration_portal.user_account(id) ON DELETE CASCADE,
    branch_code text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid,
    PRIMARY KEY (user_id, branch_code)
);
CREATE INDEX IF NOT EXISTS user_branch_code ON integration_portal.user_branch (branch_code);
