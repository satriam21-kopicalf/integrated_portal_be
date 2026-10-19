-- 019: PIC Check In (Form → Visit, 2026-10-19). A PIC checks in at the outlet he visits: the account, the branch, the
-- server time of the visit, a camera selfie (stamped with the time and the branch, a face must be detected on the
-- server) and the GPS position with its accuracy and distance to the outlet's coordinates (kopicalf Lokasi Outlet).
-- Management follows them in Monitoring → PIC Check In (app/pic_checkin.py).

CREATE TABLE IF NOT EXISTS integration_portal.pic_checkin (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid          NOT NULL,
    username          text          NOT NULL,
    full_name         text,
    role              text          NOT NULL,
    branch_code       text          NOT NULL,
    branch_name       text          NOT NULL,
    visited_at        timestamptz   NOT NULL DEFAULT now(),   -- server time of the check-in
    device_time       timestamptz,                            -- the phone's clock when the photo was taken
    latitude          numeric(9, 6) NOT NULL,
    longitude         numeric(9, 6) NOT NULL,
    accuracy_m        numeric(8, 1),
    located_at        timestamptz,                            -- when the phone took the position
    outlet_latitude   numeric(9, 6),                          -- the outlet's coordinates at the time of the visit
    outlet_longitude  numeric(9, 6),
    radius_m          integer,
    distance_m        integer,                                -- null: the outlet has no coordinates yet
    outside_radius    boolean       NOT NULL DEFAULT false,
    low_accuracy      boolean       NOT NULL DEFAULT false,   -- position less precise than 100 m
    face_count        smallint      NOT NULL CHECK (face_count > 0),
    photo             bytea         NOT NULL,
    photo_type        text          NOT NULL,
    note              text,
    device            text,
    ip                text,
    created_at        timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pic_checkin_visited_idx ON integration_portal.pic_checkin (visited_at DESC);
CREATE INDEX IF NOT EXISTS pic_checkin_branch_idx ON integration_portal.pic_checkin (branch_code, visited_at DESC);
CREATE INDEX IF NOT EXISTS pic_checkin_user_idx ON integration_portal.pic_checkin (user_id, visited_at DESC);

-- the form is ready: PICs (role user) fill it in; Monitoring stays with super admins until ticked for a role
INSERT INTO integration_portal.role_page (role, page) VALUES ('user', 'form.pic-check-in')
ON CONFLICT DO NOTHING;
