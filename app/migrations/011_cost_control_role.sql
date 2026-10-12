-- 011: role "cost_control" (Cost Control team: every branch on the dashboards, Cost Control menu incl. stock opname
-- monitor, review, settings and ESB export; no user management). Branch access of role "user" may also name
-- kopicalf facility codes (HUB WH / HUB CK without an ESB branch) for stock opname.

ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_role_check;
ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'cost_control', 'user'));

COMMENT ON COLUMN integration_portal.user_account.role IS
    'superadmin = full access; cost_control = all branches + Cost Control (stock opname review/settings/export); user = assigned branches';
COMMENT ON COLUMN integration_portal.user_branch.branch_code IS
    'ESB branch code (integration_esb.master_branches) or kopicalf facility code (kc_master.facility, e.g. HUB WH / HUB CK)';
