-- 004: dashboard user accounts and login sessions.
--
-- Users sign in with their username or email. Passwords are stored as scrypt
-- hashes (app/security.py), never in clear text. A login creates a row in
-- user_session; the browser only holds the random session token (HttpOnly
-- cookie), the database only its SHA-256 hash.
-- Roles: superadmin (full access incl. user management and platform menus)
--        user       (dashboards only: Overview, Sales Transactions)

CREATE TABLE IF NOT EXISTS integration_portal.user_account (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    username              text        NOT NULL,          -- lower case, 3-32 of a-z 0-9 . _ -
    email                 text        NOT NULL,          -- lower case
    full_name             text        NOT NULL,
    password_hash         text        NOT NULL,          -- scrypt$N$r$p$salt$hash
    role                  text        NOT NULL DEFAULT 'user',
    is_active             boolean     NOT NULL DEFAULT true,
    phone_number          text,
    job_title             text,
    department            text,
    notes                 text,
    must_change_password  boolean     NOT NULL DEFAULT false,  -- forced change at next sign-in
    last_login_at         timestamptz,
    last_login_ip         text,
    failed_login_attempts integer     NOT NULL DEFAULT 0,      -- consecutive failures
    locked_until          timestamptz,                         -- temporary lock after too many failures
    password_changed_at   timestamptz NOT NULL DEFAULT now(),
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            uuid        REFERENCES integration_portal.user_account (id) ON DELETE SET NULL,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            uuid        REFERENCES integration_portal.user_account (id) ON DELETE SET NULL,
    CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'user')),
    CONSTRAINT user_account_username_format CHECK (username ~ '^[a-z0-9._-]{3,32}$'),
    CONSTRAINT user_account_email_format CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    CONSTRAINT user_account_full_name_length CHECK (length(btrim(full_name)) BETWEEN 1 AND 120)
);
CREATE UNIQUE INDEX IF NOT EXISTS user_account_username_key ON integration_portal.user_account (lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS user_account_email_key ON integration_portal.user_account (lower(email));
CREATE INDEX IF NOT EXISTS user_account_role_active ON integration_portal.user_account (role, is_active);

COMMENT ON TABLE integration_portal.user_account IS 'Dashboard users (integrated_portal); sign-in by username or email';
COMMENT ON COLUMN integration_portal.user_account.password_hash IS 'scrypt hash, see app/security.py';
COMMENT ON COLUMN integration_portal.user_account.role IS 'superadmin = full access; user = dashboards only';

CREATE TABLE IF NOT EXISTS integration_portal.user_session (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    token_hash   text        NOT NULL UNIQUE,      -- sha256 of the cookie token
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    ip_address   text,
    user_agent   text,
    revoked_at   timestamptz                       -- sign-out, password change, deactivation
);
CREATE INDEX IF NOT EXISTS user_session_user ON integration_portal.user_session (user_id);
CREATE INDEX IF NOT EXISTS user_session_expires ON integration_portal.user_session (expires_at);

COMMENT ON TABLE integration_portal.user_session IS 'Login sessions; the browser holds the token, this table its hash';
