-- 108: Schedule of the Cost Control team (Cost Control · Clock In, 2026-11-13; app/cc_schedule.py). The leader of Cost
-- Control (an account given the page cc.schedule by a superadmin) plans the outlets each member visits per day; the
-- members are the accounts that open Cost Control Clock In. Monitoring of Cost Control Clock In reads it too.
CREATE TABLE IF NOT EXISTS integration_portal.cc_visit_schedule (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL,
    day date NOT NULL,
    branch_code text NOT NULL,
    note text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, day, branch_code)
);
CREATE INDEX IF NOT EXISTS cc_visit_schedule_day ON integration_portal.cc_visit_schedule (day);
