-- 052 (2026-11-02): Master Schedule of Calf On The Road. The shift templates (hours of a working day) and the roster:
-- one row per driver and day (a shift, off, leave or sick, at a pool). The roster is the administrative reference of
-- the attendance (the check in of the driver app against the start of the shift: on time / late / absent) and of
-- the incentive calculator (days scheduled, worked, late).
CREATE TABLE IF NOT EXISTS integration_portal.cotr_shift_template (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    start_time time NOT NULL,
    end_time   time NOT NULL,
    break_min  integer NOT NULL DEFAULT 0 CHECK (break_min >= 0),
    grace_min  integer NOT NULL DEFAULT 15 CHECK (grace_min >= 0),   -- a check in later than start + grace is late
    color      text,
    is_active  boolean NOT NULL DEFAULT true,
    sort       integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

INSERT INTO integration_portal.cotr_shift_template (code, name, start_time, end_time, break_min, color, sort) VALUES
    ('PAGI', 'Pagi', '06:00', '14:00', 60, 'amber', 1),
    ('SIANG', 'Siang', '10:00', '18:00', 60, 'blue', 2),
    ('SORE', 'Sore', '14:00', '22:00', 60, 'violet', 3)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.cotr_schedule (
    driver_code   text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day           date NOT NULL,
    status        text NOT NULL DEFAULT 'work' CHECK (status IN ('work', 'off', 'leave', 'sick')),
    template_code text REFERENCES integration_portal.cotr_shift_template (code) ON UPDATE CASCADE,
    pool_code     text,
    note          text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid,
    PRIMARY KEY (driver_code, day),
    CHECK (status <> 'work' OR template_code IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS cotr_schedule_day ON integration_portal.cotr_schedule (day);

-- the page of the schedule: given where the drivers page is
INSERT INTO integration_portal.role_page (role, page)
SELECT role, 'cotr.schedule' FROM integration_portal.role_page WHERE page = 'cotr.drivers'
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'cotr.schedule', allowed FROM integration_portal.user_page WHERE page = 'cotr.drivers'
ON CONFLICT DO NOTHING;
