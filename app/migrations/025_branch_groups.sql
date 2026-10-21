-- 025: branch groups for the monitoring (2026-10-21). The PIC Check In, Monitoring Product and Monitoring Outlet are
-- judged per class of outlet (CCI, CTG, CTGP …): a branch belongs to at most one group; groups are kept by management in
-- Monitoring → Branch groups and used by Monitoring → Overview and the filters of the monitoring pages. Seeded from the
-- ESB branch codes of the Kopi Calf outlets: CCI…, CTG…, TGP… (= CTGP).

CREATE TABLE IF NOT EXISTS integration_portal.branch_group (
    id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    code         text          NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_-]{1,20}$'),
    name         text          NOT NULL CHECK (btrim(name) <> ''),
    description  text,
    sort         integer       NOT NULL DEFAULT 0,
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now(),
    updated_by   uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.branch_group_member (
    branch_code  text          PRIMARY KEY,                -- one group per branch
    group_id     uuid          NOT NULL REFERENCES integration_portal.branch_group (id) ON DELETE CASCADE,
    added_at     timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS branch_group_member_group_idx ON integration_portal.branch_group_member (group_id);

INSERT INTO integration_portal.branch_group (code, name, description, sort) VALUES
    ('CCI', 'CCI', 'Kopi Calf (kode cabang CCI…)', 10),
    ('CTG', 'CTG', 'Kopi Calf To Go (kode cabang CTG…)', 20),
    ('CTGP', 'CTGP', 'Kopi Calf To Go (kode cabang TGP…)', 30)
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.branch_group_member (branch_code, group_id)
SELECT DISTINCT b.branch_code, g.id
FROM integration_esb.master_branches b
JOIN integration_portal.branch_group g ON g.code = CASE
        WHEN b.branch_code LIKE 'CCI%' THEN 'CCI'
        WHEN b.branch_code LIKE 'CTG%' THEN 'CTG'
        WHEN b.branch_code LIKE 'TGP%' THEN 'CTGP' END
WHERE COALESCE(b.branch_code, '') <> '' AND COALESCE(b.is_deleted, false) = false AND b.branch_name ILIKE 'Kopi Calf%'
ON CONFLICT (branch_code) DO NOTHING;
