-- 021: PIC Check In location risk (2026-10-19). Every check-in is scored for signs of a faked position (fake GPS apps,
-- browser location override, repeated / impossible positions; app/pic_checkin.py assess): risk_level + the signals, the
-- GPS readings of the form, and a manager's review (valid / invalid) of the flagged and the outside-radius check-ins.

ALTER TABLE integration_portal.pic_checkin
    ADD COLUMN IF NOT EXISTS risk_level    text        NOT NULL DEFAULT 'none'
                             CHECK (risk_level IN ('none', 'low', 'medium', 'high')),
    ADD COLUMN IF NOT EXISTS risk_signals  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS gps_samples   jsonb,
    ADD COLUMN IF NOT EXISTS review_status text        CHECK (review_status IN ('valid', 'invalid')),
    ADD COLUMN IF NOT EXISTS review_note   text,
    ADD COLUMN IF NOT EXISTS reviewed_by   text,
    ADD COLUMN IF NOT EXISTS reviewed_at   timestamptz;
CREATE INDEX IF NOT EXISTS pic_checkin_alert_idx ON integration_portal.pic_checkin (visited_at DESC)
    WHERE outside_radius OR risk_level IN ('medium', 'high');
