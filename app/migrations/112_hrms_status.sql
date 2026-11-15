-- 112: the HRMS status of the employee a person account follows (aktif / kontrak / percobaan / keluar), refreshed by
-- the hourly sync of the linked accounts (app/hrms_sync.py sync_linked, 2026-11-15). An employee who left HRMS is
-- only marked in User Accounts: the account stays active until a superadmin deactivates it (user decision).
ALTER TABLE integration_portal.user_account ADD COLUMN IF NOT EXISTS hrms_status text;
