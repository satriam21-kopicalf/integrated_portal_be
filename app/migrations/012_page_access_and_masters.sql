-- 012: page access per role (editable by superadmins) with per-user exceptions, and the job title / department
-- lists used by User Accounts (app/access.py). Superadmins always open every page; User Accounts and Activity Logs
-- stay superadmin-only and are not part of the page catalog.

CREATE TABLE IF NOT EXISTS integration_portal.role_page (
    role        text        NOT NULL CHECK (role IN ('cost_control', 'user')),
    page        text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (role, page)
);
COMMENT ON TABLE integration_portal.role_page IS 'pages (app/access.py PAGES) a role opens; superadmin = every page';

INSERT INTO integration_portal.role_page (role, page) VALUES
    ('user', 'overview'), ('user', 'sales'), ('user', 'stock-opname'),
    ('cost_control', 'overview'), ('cost_control', 'sales'), ('cost_control', 'stock-opname'),
    ('cost_control', 'cc.monitor'), ('cost_control', 'cc.reviews'), ('cost_control', 'cc.esb-export'),
    ('cost_control', 'cc.esb-compare'), ('cost_control', 'cc.lists'), ('cost_control', 'cc.groups'),
    ('cost_control', 'cc.types'), ('cost_control', 'cc.premix'), ('cost_control', 'cc.ratio'), ('cost_control', 'cc.cogs')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.user_page (
    user_id     uuid        NOT NULL REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    page        text        NOT NULL,
    allowed     boolean     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (user_id, page)
);
COMMENT ON TABLE integration_portal.user_page IS
    'exceptions to the role pages of one user: allowed = true adds a page, false removes it';

CREATE TABLE IF NOT EXISTS integration_portal.job_title (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL CHECK (btrim(name) <> ''),
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid
);
CREATE UNIQUE INDEX IF NOT EXISTS job_title_name_key ON integration_portal.job_title (lower(btrim(name)));

CREATE TABLE IF NOT EXISTS integration_portal.department (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL CHECK (btrim(name) <> ''),
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid
);
CREATE UNIQUE INDEX IF NOT EXISTS department_name_key ON integration_portal.department (lower(btrim(name)));

-- the lists start with what the accounts already carry
INSERT INTO integration_portal.job_title (name)
SELECT DISTINCT ON (lower(btrim(job_title))) btrim(job_title) FROM integration_portal.user_account
WHERE btrim(COALESCE(job_title, '')) <> '' ORDER BY lower(btrim(job_title)), btrim(job_title)
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.department (name)
SELECT DISTINCT ON (lower(btrim(department))) btrim(department) FROM integration_portal.user_account
WHERE btrim(COALESCE(department, '')) <> '' ORDER BY lower(btrim(department)), btrim(department)
ON CONFLICT DO NOTHING;
