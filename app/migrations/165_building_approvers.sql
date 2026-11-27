-- 165: Building & Renovation (2026-11-27): the RAB is approved by management only — the accounts listed here (set by a
-- superadmin on the Building page), not every superadmin. First list: the Director (Board of Director).
CREATE TABLE IF NOT EXISTS integration_portal.building_setting (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);
INSERT INTO integration_portal.building_setting (key, value, updated_by)
SELECT 'rab_approvers', coalesce(jsonb_agg(u.id::text), '[]'::jsonb), 'migration 165'
  FROM integration_portal.user_account u
 WHERE u.is_active AND u.department = 'Board Of Director' AND u.job_title = 'Director'
ON CONFLICT (key) DO NOTHING;
