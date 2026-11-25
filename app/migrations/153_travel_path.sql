-- 153: Travel Path (2026-11-25, SOP Travel Path — Foto Checklist Outlet Rev. 3): the crew of an outlet walks its zones a few
-- times a day (opening, 15.00, closing) and proves every area with a photo; a finding has a deadline by its category and
-- is closed with an after photo. One report per outlet, business day and session, shared by the accounts of the outlet.
CREATE TABLE IF NOT EXISTS integration_portal.travel_path (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_code   text        NOT NULL,
    branch_name   text        NOT NULL,
    branch_group  text,
    day           date        NOT NULL,          -- business day on the outlet's clock
    session       text        NOT NULL,
    session_label text        NOT NULL,
    status        text        NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SUBMITTED')),
    crew          text,
    supervisor    text,
    started_by    uuid        NOT NULL,
    started_name  text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    submitted_by  uuid,
    submitted_name text,
    submitted_at  timestamptz,
    window_end    timestamptz,
    late          boolean     NOT NULL DEFAULT false,   -- sent after the session window
    report_late   boolean     NOT NULL DEFAULT false,   -- sent later than the report minutes after the last photo
    answers       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    counts        jsonb,
    score         numeric(5, 1),
    band          text,
    latitude      numeric(9, 6),
    longitude     numeric(9, 6),
    accuracy_m    numeric(8, 1),
    distance_m    integer,
    outside_radius boolean    NOT NULL DEFAULT false,
    UNIQUE (branch_code, day, session)
);
CREATE INDEX IF NOT EXISTS travel_path_day_idx ON integration_portal.travel_path (day DESC, branch_code);

CREATE TABLE IF NOT EXISTS integration_portal.travel_path_finding (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id     uuid        NOT NULL REFERENCES integration_portal.travel_path (id) ON DELETE CASCADE,
    branch_code   text        NOT NULL,
    branch_name   text        NOT NULL,
    day           date        NOT NULL,
    session       text        NOT NULL,
    item_key      text        NOT NULL,
    label         text        NOT NULL,
    zone          text,
    category      text,
    note          text,
    due_at        timestamptz,
    status        text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'FIXED')),
    fix_note      text,
    fixed_by      text,
    fixed_at      timestamptz,
    reopened_note text,
    reopened_by   text,
    reopened_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, item_key)
);
CREATE INDEX IF NOT EXISTS travel_path_finding_open_idx ON integration_portal.travel_path_finding (branch_code) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS travel_path_finding_day_idx ON integration_portal.travel_path_finding (day DESC);

CREATE TABLE IF NOT EXISTS integration_portal.travel_path_photo (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id     uuid        NOT NULL REFERENCES integration_portal.travel_path (id) ON DELETE CASCADE,
    item_key      text        NOT NULL,
    kind          text        NOT NULL DEFAULT 'MAIN' CHECK (kind IN ('MAIN', 'AFTER')),
    photo         bytea       NOT NULL,
    photo_type    text        NOT NULL,
    taken_at      timestamptz,
    uploaded_at   timestamptz NOT NULL DEFAULT now(),
    latitude      numeric(9, 6),
    longitude     numeric(9, 6),
    distance_m    integer,
    outside_radius boolean    NOT NULL DEFAULT false,
    uploaded_by   uuid        NOT NULL
);
CREATE INDEX IF NOT EXISTS travel_path_photo_report_idx ON integration_portal.travel_path_photo (report_id, item_key);

-- one row: sessions, score and deadlines (app/travel_path.py DEFAULTS when empty)
CREATE TABLE IF NOT EXISTS integration_portal.travel_path_setting (
    id         integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    config     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- the outlet accounts fill it (role outlet) in Operations; Monitoring goes to whoever opens Monitoring Outlet
INSERT INTO integration_portal.role_page (role, page) VALUES ('outlet', 'form.travel') ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.role_page (role, page) SELECT role, 'mon.travel' FROM integration_portal.role_page WHERE page = 'mon.outlet' ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'mon.travel', allowed FROM integration_portal.user_page WHERE page = 'mon.outlet' ON CONFLICT DO NOTHING;
-- not the HUB WH / HUB CK / Wasgee accounts: the SOP is for the Kopi Calf outlets (the test branches DMY-… keep it)
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT u.id, 'form.travel', false FROM integration_portal.user_account u
 WHERE u.role = 'outlet' AND NOT EXISTS (
       SELECT 1 FROM integration_portal.user_branch ub
        WHERE ub.user_id = u.id AND (ub.branch_code LIKE 'DMY-%' OR EXISTS (
              SELECT 1 FROM integration_esb.master_branches b WHERE b.branch_code = ub.branch_code AND b.branch_name ILIKE 'Kopi Calf%')))
ON CONFLICT DO NOTHING;
