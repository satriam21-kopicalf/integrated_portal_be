-- 036: Role "outlet" and the pages of the PICs (2026-10-28, user request).
--   * role "outlet" (Outlet / Hub): the shared accounts of the outlets, HUB CK and HUB WH (outlet.*, hubck.*, hubwh.*,
--     wasgee.*, wh.*, ck.pusat, roastery.pusat, and the staff with job title HUB WH / HUB CK) open Stock Opname and
--     Waste only; their per-user exceptions (migrations 033, 034) are no longer needed.
--   * role "user" (Area Supervisor / PIC): Dashboard, Sales Transactions, Stock Opname, Waste, the Guide and every form
--     (PIC Check In, Product, Outlet, Damaged Equipment, Pengajuan; a later form is added to the role by its migration).
--     The PICs (job title Area Supervisor, or any account that fills the PIC Check In) lose their exceptions that removed
--     one of those pages; Area Supervisors (and PICs without a job title) lose Cost Control Monitor / Review. Other
--     exceptions (Monitoring, Cost Control entry …) stay.
--   * every other account of role "user" keeps exactly its pages: the pages the role gains are removed for it.
-- Every exception and role changed is kept in user_page_backup_20261028c / user_role_backup_20261028c.

ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_role_check;
ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'cost_control', 'user', 'outlet'));
ALTER TABLE integration_portal.role_page DROP CONSTRAINT IF EXISTS role_page_role_check;
ALTER TABLE integration_portal.role_page ADD CONSTRAINT role_page_role_check CHECK (role IN ('cost_control', 'user', 'outlet'));
COMMENT ON COLUMN integration_portal.user_account.role IS
    'superadmin = full access; cost_control = all branches + Cost Control; user = Area Supervisor / PIC, assigned branches; outlet = shared outlet / hub accounts, assigned branches';

CREATE TEMP TABLE facility ON COMMIT DROP AS
    SELECT id FROM integration_portal.user_account
    WHERE role = 'user' AND (split_part(username, '.', 1) IN ('outlet', 'hubck', 'hubwh', 'wasgee', 'wh')
                             OR username IN ('ck.pusat', 'roastery.pusat') OR job_title IN ('HUB WH', 'HUB CK'));
CREATE TEMP TABLE pic ON COMMIT DROP AS
    SELECT u.id, (u.job_title = 'Area Supervisor' OR u.job_title IS NULL) AS supervisor FROM integration_portal.user_account u
    WHERE u.role = 'user' AND u.id NOT IN (SELECT id FROM facility)
      AND (u.job_title = 'Area Supervisor'
           OR NOT EXISTS (SELECT 1 FROM integration_portal.user_page p WHERE p.user_id = u.id AND p.page = 'form.pic-check-in' AND NOT p.allowed));
CREATE TEMP TABLE user_role_page ON COMMIT DROP AS
    SELECT unnest(ARRAY['overview', 'sales', 'stock-opname', 'waste', 'guide', 'form.pic-check-in', 'form.product', 'form.outlet',
                        'form.damaged-equipment', 'form.pengajuan']) AS page;
-- the pages role "user" gains now (before this migration it had overview, sales, stock-opname, guide and 4 forms)
CREATE TEMP TABLE gained ON COMMIT DROP AS
    SELECT page FROM user_role_page WHERE page NOT IN (SELECT page FROM integration_portal.role_page WHERE role = 'user');

CREATE TABLE IF NOT EXISTS integration_portal.user_page_backup_20261028c AS
    SELECT p.* FROM integration_portal.user_page p JOIN integration_portal.user_account u ON u.id = p.user_id WHERE u.role = 'user';
CREATE TABLE IF NOT EXISTS integration_portal.user_role_backup_20261028c AS
    SELECT id, username, role FROM integration_portal.user_account WHERE id IN (SELECT id FROM facility);

-- outlet / hub accounts: role "outlet", no exceptions
DELETE FROM integration_portal.user_page WHERE user_id IN (SELECT id FROM facility);
UPDATE integration_portal.user_account SET role = 'outlet', updated_at = now() WHERE id IN (SELECT id FROM facility);

-- other accounts of role "user": keep their pages as they are (the role's new pages are removed for them)
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT u.id, g.page, false FROM integration_portal.user_account u CROSS JOIN gained g
    WHERE u.role = 'user' AND u.id NOT IN (SELECT id FROM pic)
ON CONFLICT (user_id, page) DO NOTHING;

-- PICs: every page of the role; Area Supervisors without Cost Control Monitor / Review; exceptions equal to the role go
DELETE FROM integration_portal.user_page p USING pic
    WHERE p.user_id = pic.id AND p.page IN (SELECT page FROM user_role_page);
DELETE FROM integration_portal.user_page p USING pic
    WHERE p.user_id = pic.id AND pic.supervisor AND p.page IN ('cc.monitor', 'cc.reviews');

-- the role pages
DELETE FROM integration_portal.role_page WHERE role IN ('user', 'outlet');
INSERT INTO integration_portal.role_page (role, page) SELECT 'user', page FROM user_role_page;
INSERT INTO integration_portal.role_page (role, page) VALUES ('outlet', 'stock-opname'), ('outlet', 'waste');
