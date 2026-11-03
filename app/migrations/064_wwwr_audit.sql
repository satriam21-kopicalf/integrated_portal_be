-- 064 (2026-11-03): Form WW-WR, the field audit of an outlet by the SWAT team (app/wwwr.py). One form with every item
-- of Monitoring Product and Monitoring Outlet (form 'wwwr' in visit_form, item keys "product:…" / "outlet:…"). The
-- management schedules an audit (outlet, day, a lead and the members); only the members open it in Operations and fill
-- the one shared visit, the lead sends it. Monitoring WW-WR compares it item by item with the Product and Outlet visits
-- of the PIC of the outlet. Pages form.wwwr (given per account, the SWAT team) and mon.wwwr (management).
ALTER TABLE integration_portal.visit_form DROP CONSTRAINT IF EXISTS visit_form_form_check;
ALTER TABLE integration_portal.visit_form ADD CONSTRAINT visit_form_form_check CHECK (form IN ('product', 'outlet', 'wwwr'));

CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_code text        NOT NULL,
    branch_name text        NOT NULL,
    day         date        NOT NULL,
    lead_id     uuid        NOT NULL,
    note        text,
    status      text        NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
    visit_id    uuid        REFERENCES integration_portal.visit_form (id) ON DELETE SET NULL,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
-- one audit per outlet per day
CREATE UNIQUE INDEX IF NOT EXISTS wwwr_audit_one_per_day ON integration_portal.wwwr_audit (branch_code, day) WHERE status <> 'cancelled';
CREATE INDEX IF NOT EXISTS wwwr_audit_day ON integration_portal.wwwr_audit (day DESC);

CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_member (
    audit_id uuid NOT NULL REFERENCES integration_portal.wwwr_audit (id) ON DELETE CASCADE,
    user_id  uuid NOT NULL,
    PRIMARY KEY (audit_id, user_id)
);
CREATE INDEX IF NOT EXISTS wwwr_audit_member_user ON integration_portal.wwwr_audit_member (user_id);
