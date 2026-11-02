-- 055 (2026-11-02): Waste improvements of Cost Control.
-- 1. Categories per form: scope 'waste' (Form Waste, also premix) or 'usage' (Usage Management: Customer Supplies,
--    Consumable Supplies); Cost Control adds / renames / switches them off (page cc.waste-review), outlets only pick.
-- 2. A signature before submitting: every waste entry and every Usage Management line keeps the signature of the
--    person who submitted it.
-- 3. Investigate: besides approve / reject, Cost Control opens an investigation of one or more entries; the report
--    follows docs/Form Investigasi Waste (FM-QAQC-03: A category, B object & chronology, C root cause, D corrective /
--    preventive action, three signatures). The entries wait in status 'investigating' until approved or rejected.
ALTER TABLE integration_portal.waste_category ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'waste';
DO $$ BEGIN
    ALTER TABLE integration_portal.waste_category ADD CONSTRAINT waste_category_scope_check CHECK (scope IN ('waste', 'usage'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE integration_portal.waste_category DROP CONSTRAINT IF EXISTS waste_category_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS waste_category_scope_name ON integration_portal.waste_category (scope, lower(name));
INSERT INTO integration_portal.waste_category (scope, name, sort) VALUES
    ('usage', 'Customer Supplies', 1), ('usage', 'Consumable Supplies', 2)
ON CONFLICT DO NOTHING;

ALTER TABLE integration_portal.waste_entry
    ADD COLUMN IF NOT EXISTS signature bytea,
    ADD COLUMN IF NOT EXISTS signature_type text,
    ADD COLUMN IF NOT EXISTS investigation_id uuid;
ALTER TABLE integration_portal.usage_count
    ADD COLUMN IF NOT EXISTS signature bytea,
    ADD COLUMN IF NOT EXISTS signature_type text;

ALTER TABLE integration_portal.waste_entry DROP CONSTRAINT IF EXISTS waste_entry_status_check;
ALTER TABLE integration_portal.waste_entry ADD CONSTRAINT waste_entry_status_check
    CHECK (status IN ('submitted', 'investigating', 'approved', 'rejected'));

CREATE SEQUENCE IF NOT EXISTS integration_portal.waste_investigation_no;
CREATE TABLE IF NOT EXISTS integration_portal.waste_investigation (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    report_no   integer     NOT NULL DEFAULT nextval('integration_portal.waste_investigation_no') UNIQUE,
    branch_code text        NOT NULL,
    branch_name text        NOT NULL,
    status      text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    report      jsonb       NOT NULL DEFAULT '{}',   -- the fields of the form (sections A–D, names of the signers)
    signatures  jsonb       NOT NULL DEFAULT '{}',   -- {made|checked|approved: {name, role, image (data URL), at}}
    created_by  text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS waste_entry_investigation ON integration_portal.waste_entry (investigation_id);
