-- 147: personal notifications (2026-11-24, app/alerts.py): an alert of one account (the overdue proof of a Fund Request, to
-- its requester and to Finance). NULL = the Cost Control alerts, as before.
ALTER TABLE integration_portal.cc_alert ADD COLUMN IF NOT EXISTS user_id uuid;
CREATE INDEX IF NOT EXISTS cc_alert_user ON integration_portal.cc_alert (user_id, created_at DESC) WHERE user_id IS NOT NULL;
