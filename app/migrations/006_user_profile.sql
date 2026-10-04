-- 006: self-service profile.
--
-- A superadmin only creates the login (username, email, password, role);
-- the user completes their identity in "My profile". full_name is therefore
-- optional until then (the UI shows the username meanwhile).

ALTER TABLE integration_portal.user_account
    ALTER COLUMN full_name DROP NOT NULL,
    DROP CONSTRAINT IF EXISTS user_account_full_name_length,
    ADD COLUMN IF NOT EXISTS employee_number  text,   -- staff ID (NIK karyawan)
    ADD COLUMN IF NOT EXISTS gender           text,   -- male | female
    ADD COLUMN IF NOT EXISTS birth_date       date,
    ADD COLUMN IF NOT EXISTS address          text,
    ADD COLUMN IF NOT EXISTS city             text,
    ADD COLUMN IF NOT EXISTS work_branch_code text,   -- outlet / office (integration_esb.master_branches.branch_code)
    ADD COLUMN IF NOT EXISTS profile_updated_at timestamptz;  -- last change made by the user themself

ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_full_name_length CHECK (full_name IS NULL OR length(btrim(full_name)) BETWEEN 1 AND 120),
    ADD CONSTRAINT user_account_gender_check CHECK (gender IS NULL OR gender IN ('male', 'female')),
    ADD CONSTRAINT user_account_birth_date_check CHECK (birth_date IS NULL OR birth_date >= DATE '1900-01-01');  -- not in the future: checked by the API

COMMENT ON COLUMN integration_portal.user_account.work_branch_code IS 'work location: integration_esb.master_branches.branch_code';
