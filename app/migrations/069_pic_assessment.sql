-- 069: PIC assessment after the Store Performance workbook (2026-11-04, user decisions):
--   * the check-in deadline of a branch group (the first check-in of the PIC's day by then is on time): CCI 10:00,
--     CTGP 10:15, CTG 10:30; Monitoring OPS → Branch groups changes it;
--   * per outlet: its own deadline (instead of the group's) and its time zone — the deadline is in the outlet's own
--     clock (Renon Bali in WITA is on time until 10:30 WITA = 09:30 WIB) — integration_portal.outlet_setting;
--   * which Product / Outlet items each branch group is asked (on top of the defaults of app/visit_forms.py):
--     integration_portal.form_item_scope;
--   * the monthly net sales target of every outlet (the sales part of the PIC score): integration_portal.outlet_sales_target.

ALTER TABLE integration_portal.branch_group ADD COLUMN IF NOT EXISTS checkin_deadline time;
UPDATE integration_portal.branch_group SET checkin_deadline = '10:00' WHERE code = 'CCI' AND checkin_deadline IS NULL;
UPDATE integration_portal.branch_group SET checkin_deadline = '10:15' WHERE code = 'CTGP' AND checkin_deadline IS NULL;
UPDATE integration_portal.branch_group SET checkin_deadline = '10:30' WHERE code = 'CTG' AND checkin_deadline IS NULL;

CREATE TABLE IF NOT EXISTS integration_portal.outlet_setting (
    branch_code       text PRIMARY KEY,
    checkin_deadline  time,          -- NULL: the deadline of the outlet's branch group
    timezone          text CHECK (timezone IN ('Asia/Jakarta', 'Asia/Makassar', 'Asia/Jayapura')),  -- NULL: WIB
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        uuid
);
-- the outlets in central Indonesia (WITA) known by name; the rest is set in Monitoring OPS → Branch groups
INSERT INTO integration_portal.outlet_setting (branch_code, timezone)
SELECT DISTINCT b.branch_code, 'Asia/Makassar'
FROM integration_esb.master_branches b
WHERE COALESCE(b.branch_code, '') <> '' AND b.branch_name ILIKE 'Kopi Calf%'
  AND b.branch_name ~* '(bali|renon|denpasar|badung|kuta|ubud|lombok|mataram|makassar|balikpapan|samarinda|banjarmasin|manado|kupang)'
ON CONFLICT (branch_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.form_item_scope (
    form        text NOT NULL CHECK (form IN ('product', 'outlet')),
    item_key    text NOT NULL,
    group_code  text NOT NULL,
    shown       boolean NOT NULL,   -- true: asked at the outlets of the group, false: not asked (and not assessed)
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (form, item_key, group_code)
);

CREATE TABLE IF NOT EXISTS integration_portal.outlet_sales_target (
    branch_code  text NOT NULL,
    month        date NOT NULL CHECK (extract(day FROM month) = 1),
    target       numeric(18, 2) NOT NULL CHECK (target > 0),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   uuid,
    PRIMARY KEY (branch_code, month)
);
