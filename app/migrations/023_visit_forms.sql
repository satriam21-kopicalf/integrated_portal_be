-- 023: Form → Monitoring Product / Monitoring Outlet (2026-10-20, workbook "Form Visit"). A PIC's visit of one outlet:
-- a draft saved as it is filled (one per form, account and branch), then submitted; every item rated UNDER / MEET /
-- EXCEED with a note, camera photos per item (stamped in the browser; position and distance to the outlet kept), the
-- position at submit against the outlet's coordinates (app/visit_forms.py). Pages form.outlet / form.product (role
-- user fills them) and mon.outlet / mon.product (management, ticked in User Accounts → Roles & pages).

CREATE TABLE IF NOT EXISTS integration_portal.visit_form (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    form              text          NOT NULL CHECK (form IN ('product', 'outlet')),
    status            text          NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SUBMITTED')),
    user_id           uuid          NOT NULL,
    username          text          NOT NULL,
    full_name         text,
    role              text          NOT NULL,
    branch_code       text          NOT NULL,
    branch_name       text          NOT NULL,
    started_at        timestamptz   NOT NULL DEFAULT now(),
    updated_at        timestamptz   NOT NULL DEFAULT now(),
    submitted_at      timestamptz,
    answers           jsonb         NOT NULL DEFAULT '{}'::jsonb,   -- {item key: {rating, note}}
    counts            jsonb,                                          -- {under, meet, exceed, rated, total, score}
    score             numeric(5, 1),                                  -- % of the rated items MEET or EXCEED
    latitude          numeric(9, 6),                                  -- position at submit
    longitude         numeric(9, 6),
    accuracy_m        numeric(8, 1),
    outlet_latitude   numeric(9, 6),
    outlet_longitude  numeric(9, 6),
    radius_m          integer,
    distance_m        integer,
    outside_radius    boolean       NOT NULL DEFAULT false,
    low_accuracy      boolean       NOT NULL DEFAULT false,
    device            text,
    ip                text
);
CREATE UNIQUE INDEX IF NOT EXISTS visit_form_one_draft ON integration_portal.visit_form (form, user_id, branch_code)
    WHERE status = 'DRAFT';
CREATE INDEX IF NOT EXISTS visit_form_submitted_idx ON integration_portal.visit_form (form, submitted_at DESC)
    WHERE status = 'SUBMITTED';
CREATE INDEX IF NOT EXISTS visit_form_branch_idx ON integration_portal.visit_form (branch_code, submitted_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.visit_form_photo (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    visit_id          uuid          NOT NULL REFERENCES integration_portal.visit_form (id) ON DELETE CASCADE,
    item_key          text          NOT NULL,
    photo             bytea         NOT NULL,
    photo_type        text          NOT NULL,
    taken_at          timestamptz,                                    -- the phone's clock when the photo was taken
    uploaded_at       timestamptz   NOT NULL DEFAULT now(),
    latitude          numeric(9, 6),
    longitude         numeric(9, 6),
    distance_m        integer,                                        -- to the outlet's coordinates
    outside_radius    boolean       NOT NULL DEFAULT false,
    uploaded_by       uuid          NOT NULL
);
CREATE INDEX IF NOT EXISTS visit_form_photo_visit_idx ON integration_portal.visit_form_photo (visit_id, item_key);

-- the PICs (role user) fill both forms; the monitoring pages stay with super admins until ticked for a role
INSERT INTO integration_portal.role_page (role, page) VALUES ('user', 'form.outlet'), ('user', 'form.product')
ON CONFLICT DO NOTHING;
-- ...but not the branch accounts of the stock opname (1 account = 1 branch, 2026-10-20: Dashboard, Sales, Stock Opname)
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT u.id, p.page, false FROM integration_portal.user_account u
CROSS JOIN (VALUES ('form.outlet'), ('form.product')) AS p (page)
WHERE u.role = 'user' AND u.notes LIKE 'Akun Stock Opname cabang%'
ON CONFLICT (user_id, page) DO NOTHING;
