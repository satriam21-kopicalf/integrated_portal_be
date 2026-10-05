-- 009: activity log (superadmin only, GET /api/activity).
--
-- Who did what in the dashboard: sign-in/out (incl. failed attempts), page views and
-- filters, transaction details opened, Excel exports (requested, finished/failed,
-- downloaded), user and profile changes, and refused access. Written by the backend
-- (app/activity.py); page views and filters come from the dashboard via
-- POST /api/activity/events. Kept for 400 days.

CREATE TABLE IF NOT EXISTS integration_portal.activity_log (
    id          bigserial   PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    user_id     uuid,                  -- NULL for a failed sign-in with an unknown username
    username    text,
    role        text,
    category    text        NOT NULL,  -- auth | page | filter | transaction | export | user | profile | access
    action      text        NOT NULL,  -- e.g. export.create, page.view, auth.login_failed
    status      text        NOT NULL DEFAULT 'ok',  -- ok | failed | denied
    page        text,                  -- dashboard page the action came from
    summary     text,                  -- one readable line
    details     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ip          text,
    user_agent  text
);
CREATE INDEX IF NOT EXISTS activity_log_created ON integration_portal.activity_log (created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_user ON integration_portal.activity_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS activity_log_category ON integration_portal.activity_log (category, created_at DESC);
