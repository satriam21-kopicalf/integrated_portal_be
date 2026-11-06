-- 073: Cost Control Clock In / Clock Out (2026-11-06). The Cost Control check in (kind surveyor, migration 039) is now
-- the Clock In of a visit; the surveyor clocks out when leaving the outlet with the same evidence: a selfie (a face is
-- detected on the server) and the GPS position against the outlet's coordinates kept at the clock in (outside the
-- radius: a reason). One open visit at a time: a new clock in waits for the clock out of the open one (app/pic_checkin.py).

ALTER TABLE integration_portal.pic_checkin
    ADD COLUMN IF NOT EXISTS out_at            timestamptz,     -- server time of the clock out
    ADD COLUMN IF NOT EXISTS out_device_time   timestamptz,
    ADD COLUMN IF NOT EXISTS out_latitude      numeric(9, 6),
    ADD COLUMN IF NOT EXISTS out_longitude     numeric(9, 6),
    ADD COLUMN IF NOT EXISTS out_accuracy_m    numeric(8, 1),
    ADD COLUMN IF NOT EXISTS out_located_at    timestamptz,
    ADD COLUMN IF NOT EXISTS out_distance_m    integer,
    ADD COLUMN IF NOT EXISTS out_outside_radius boolean,
    ADD COLUMN IF NOT EXISTS out_low_accuracy  boolean,
    ADD COLUMN IF NOT EXISTS out_face_count    smallint,
    ADD COLUMN IF NOT EXISTS out_photo         bytea,
    ADD COLUMN IF NOT EXISTS out_photo_type    text,
    ADD COLUMN IF NOT EXISTS out_note          text,
    ADD COLUMN IF NOT EXISTS out_device        text,
    ADD COLUMN IF NOT EXISTS out_ip            text,
    ADD COLUMN IF NOT EXISTS out_risk_level    text,
    ADD COLUMN IF NOT EXISTS out_risk_signals  jsonb;

CREATE INDEX IF NOT EXISTS pic_checkin_open_idx ON integration_portal.pic_checkin (user_id, visited_at DESC)
    WHERE kind = 'surveyor' AND out_at IS NULL;
