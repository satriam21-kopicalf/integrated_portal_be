-- 164: Building & Renovation projects (2026-11-27, Building team; page mon.building, RAB approval mon.building-approve):
-- a project (new outlet, renovation, major maintenance) with its RAB (sections → lines, from the material catalog or typed,
-- with the actual of every line), the approved versions of the RAB, its stages (weight, plan dates, progress) and the
-- progress reports with photos. Numbers BP-YYMM-NNNN.
CREATE TABLE IF NOT EXISTS integration_portal.building_project (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    number         text        UNIQUE,
    kind           text        NOT NULL CHECK (kind IN ('NEW_OUTLET', 'RENOVATION', 'MAINTENANCE')),
    name           text        NOT NULL,
    branch_code    text,                        -- an existing outlet (renovation), else the planned outlet below
    location_name  text,
    address        text,
    city           text,
    area_m2        numeric(10,2),
    business_unit  text,                        -- Kopi Calf, Wasgee …
    pic_name       text,
    contractor     text,
    plan_start     date,
    plan_end       date,
    actual_start   date,
    actual_end     date,
    budget         numeric(16,2),
    status         text        NOT NULL DEFAULT 'PLANNING'
                   CHECK (status IN ('PLANNING', 'IN_PROGRESS', 'HANDOVER', 'DONE', 'ON_HOLD', 'CANCELLED')),
    rab_status     text        NOT NULL DEFAULT 'DRAFT' CHECK (rab_status IN ('DRAFT', 'SUBMITTED', 'APPROVED')),
    rab_version    integer     NOT NULL DEFAULT 0,  -- the last approved version
    note           text,
    created_by     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text
);

CREATE TABLE IF NOT EXISTS integration_portal.building_counter (month text PRIMARY KEY, last integer NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS integration_portal.building_rab_section (
    id         serial PRIMARY KEY,
    project_id uuid    NOT NULL REFERENCES integration_portal.building_project(id) ON DELETE CASCADE,
    sort       integer NOT NULL DEFAULT 0,
    name       text    NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_portal.building_rab_line (
    id            serial PRIMARY KEY,
    project_id    uuid    NOT NULL REFERENCES integration_portal.building_project(id) ON DELETE CASCADE,
    section_id    integer NOT NULL REFERENCES integration_portal.building_rab_section(id) ON DELETE CASCADE,
    sort          integer NOT NULL DEFAULT 0,
    kind          text    NOT NULL DEFAULT 'MATERIAL' CHECK (kind IN ('MATERIAL', 'LABOUR', 'OTHER')),
    material_id   integer REFERENCES integration_portal.building_material(id) ON DELETE SET NULL,
    description   text    NOT NULL,
    spec          text,
    unit          text,
    qty           numeric(14,3) NOT NULL DEFAULT 1,
    unit_price    numeric(16,2) NOT NULL DEFAULT 0,
    price_on      date,
    vendor_id     integer REFERENCES integration_portal.building_vendor(id) ON DELETE SET NULL,
    vendor_text   text,
    note          text,
    actual_qty    numeric(14,3),
    actual_amount numeric(16,2),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text
);
CREATE INDEX IF NOT EXISTS building_rab_line_project ON integration_portal.building_rab_line (project_id, section_id, sort);

CREATE TABLE IF NOT EXISTS integration_portal.building_rab_version (
    project_id  uuid        NOT NULL REFERENCES integration_portal.building_project(id) ON DELETE CASCADE,
    version     integer     NOT NULL,
    total       numeric(16,2) NOT NULL,
    data        jsonb       NOT NULL,       -- the sections and lines as approved
    approved_by text,
    approved_at timestamptz NOT NULL DEFAULT now(),
    note        text,
    PRIMARY KEY (project_id, version)
);

CREATE TABLE IF NOT EXISTS integration_portal.building_stage (
    id         serial PRIMARY KEY,
    project_id uuid    NOT NULL REFERENCES integration_portal.building_project(id) ON DELETE CASCADE,
    sort       integer NOT NULL DEFAULT 0,
    name       text    NOT NULL,
    weight     numeric(6,2) NOT NULL DEFAULT 0,  -- % of the whole work
    plan_start date,
    plan_end   date,
    progress   numeric(5,2) NOT NULL DEFAULT 0, -- 0-100
    done_on    date
);

CREATE TABLE IF NOT EXISTS integration_portal.building_progress (
    id         serial PRIMARY KEY,
    project_id uuid    NOT NULL REFERENCES integration_portal.building_project(id) ON DELETE CASCADE,
    stage_id   integer REFERENCES integration_portal.building_stage(id) ON DELETE SET NULL,
    progress   numeric(5,2),                    -- the stage's progress reported
    note       text,
    reported_on date   NOT NULL,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration_portal.building_photo (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    progress_id integer NOT NULL REFERENCES integration_portal.building_progress(id) ON DELETE CASCADE,
    path        text    NOT NULL,
    mime        text    NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
