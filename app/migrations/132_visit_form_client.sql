-- 132: where a visit was sent from (2026-11-21, app/visit_forms.py client_of): ATTENDANCE_APP (the native attendance app),
-- ATTENDANCE_PWA (the attendance app in a browser) or OPERATIONS. Visits sent before are filled only where the stored
-- user agent tells it for sure (the native app's okhttp); the rest stay unknown rather than guessed.
ALTER TABLE integration_portal.visit_form ADD COLUMN IF NOT EXISTS client text
    CHECK (client IS NULL OR client IN ('ATTENDANCE_APP', 'ATTENDANCE_PWA', 'OPERATIONS'));
UPDATE integration_portal.visit_form SET client = 'ATTENDANCE_APP'
 WHERE client IS NULL AND status = 'SUBMITTED' AND device ILIKE '%okhttp%';
