-- 115: kind of account (2026-11-17, user request). A person account (role user / cost_control / admin / driver /
-- cotr_admin) is made from its HRMS employee; a shared account (one sign-in used by a team, e.g. administrator.cotr)
-- or a test account says so and is never linked to HRMS, never synced and never counted in the person scores.
-- Sign-in is not affected: only User Accounts create / edit read the kind.
ALTER TABLE integration_portal.user_account ADD COLUMN IF NOT EXISTS account_kind text NOT NULL DEFAULT 'person';
ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_kind_check;
ALTER TABLE integration_portal.user_account ADD CONSTRAINT user_account_kind_check
    CHECK (account_kind IN ('person', 'shared', 'test'));

-- the person-role accounts without an HRMS employee today are shared ones (administrator.cotr on 2026-11-17)
UPDATE integration_portal.user_account SET account_kind = 'shared'
 WHERE role IN ('user', 'cost_control', 'admin', 'driver', 'cotr_admin') AND hrms_employee_id IS NULL
   AND account_kind = 'person';
