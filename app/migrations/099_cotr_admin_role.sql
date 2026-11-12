-- 099: the COTR admin (2026-11-13): an account of the Calf On The Road line that opens every COTR page and manages the
-- accounts of the COTR line only (User Accounts: create, change, unlock, delete; roles user / outlet / driver / COTR
-- admin; COTR pages only). Every account of the COTR line but the global superadmins opens the COTR pages and the
-- guide only — never the Dashboard or Sales Transactions of Kopi Calf / Wasgee (app/access.py pages_for).
ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_role_check;
ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'cost_control', 'user', 'outlet', 'driver', 'cotr_admin'));
UPDATE integration_portal.user_account SET business_line = 'cotr' WHERE role = 'cotr_admin' AND business_line IS DISTINCT FROM 'cotr';
-- the superadmin accounts of the COTR line ("superadmin COTR", e.g. administrator.cotr) become COTR admins, as long as
-- a global superadmin stays active (user decision 2026-11-13: no COTR account opens the Dashboard / Sales)
UPDATE integration_portal.user_account u SET role = 'cotr_admin'
 WHERE u.role = 'superadmin' AND u.business_line = 'cotr'
   AND EXISTS (SELECT 1 FROM integration_portal.user_account g
                WHERE g.role = 'superadmin' AND g.is_active AND coalesce(g.business_line, 'kopicalf') <> 'cotr');
