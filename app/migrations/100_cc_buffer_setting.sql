-- 100: Estimasi Belanja per outlet (Cost Control, 2026-11-13; app/purchase_plan.py). The buffer of an outlet is
-- recommended from its own data (cover days from the gaps between its goods receipts, loss % from its approved waste,
-- safety % from the swing of its sales); Cost Control may set any part for an outlet, with a note. A NULL part stays
-- recommended; no row = everything recommended.
CREATE TABLE IF NOT EXISTS integration_portal.cc_buffer_setting (
    branch_code text PRIMARY KEY,
    cover_days integer CHECK (cover_days BETWEEN 1 AND 31),
    loss_pct numeric(6, 2) CHECK (loss_pct BETWEEN 0 AND 100),
    safety_pct numeric(6, 2) CHECK (safety_pct BETWEEN 0 AND 100),
    note text,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- the page for the roles that open the Sales Calculator or COGS & Usage
INSERT INTO integration_portal.role_page (role, page)
SELECT DISTINCT r.role, 'cc.purchase-plan' FROM integration_portal.role_page r WHERE r.page IN ('cc.sales-calc', 'cc.cogs')
ON CONFLICT DO NOTHING;
