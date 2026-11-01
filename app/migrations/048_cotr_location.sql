-- 048 (2026-11-01): where the COTR drivers are. While a shift is open the driver app sends the GPS position of the
-- phone (Geolocation API, high accuracy) every ~30 s or 25 m moved, in batches (kept on the phone while offline); the
-- team follows the last position of every driver and the track of one in Monitoring COTR → Driver location.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_location (
    id          bigserial PRIMARY KEY,
    driver_code text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    shift_id    uuid REFERENCES integration_portal.cotr_shift (id) ON DELETE CASCADE,
    at          timestamptz NOT NULL,                   -- when the phone took the position
    received_at timestamptz NOT NULL DEFAULT now(),
    latitude    numeric(9,6) NOT NULL,
    longitude   numeric(9,6) NOT NULL,
    accuracy_m  numeric(10,1),
    speed_ms    numeric(8,2),
    heading     numeric(5,1),
    battery     smallint                                  -- percent, when the browser tells it
);
CREATE INDEX IF NOT EXISTS cotr_location_driver_at ON integration_portal.cotr_location (driver_code, at DESC);
CREATE INDEX IF NOT EXISTS cotr_location_shift ON integration_portal.cotr_location (shift_id, at);
