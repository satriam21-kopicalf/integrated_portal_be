-- 105: role Admin (2026-11-13). Like a superadmin for the menus the superadmin gives it (role pages of "admin" in the
-- access settings + exceptions per account, set by a superadmin only); it sees every branch; it manages the accounts of
-- role User only (create, edit, deactivate) and gives them only pages it opens itself; it never changes its own access.
-- It starts with no page: the superadmin chooses them.
ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_role_check;
ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'admin', 'cost_control', 'user', 'outlet', 'driver', 'cotr_admin'));
