-- 110: link of a person account to its HRMS employee (2026-11-15, app/hrms_sync.py). Job title and department of
-- the accounts of role user / cost_control / admin follow HRMS (sk_hrms.positions.title, sk_hrms.org_units.name);
-- outlet / hub, driver and superadmin accounts are never touched. Login, role and pages do not read these columns.
ALTER TABLE integration_portal.user_account
    ADD COLUMN IF NOT EXISTS hrms_employee_id text,
    ADD COLUMN IF NOT EXISTS hrms_linked_by text,      -- name | email | employee_no | manual
    ADD COLUMN IF NOT EXISTS hrms_synced_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS user_account_hrms_employee_uq
    ON integration_portal.user_account (hrms_employee_id) WHERE hrms_employee_id IS NOT NULL;

-- job title / department before the first sync, to restore them if needed
CREATE TABLE IF NOT EXISTS integration_portal.user_account_profile_bak_20261115 AS
SELECT id, job_title, department, now() AS saved_at FROM integration_portal.user_account;
