-- 080: Calf On The Road masters (2026-11-08).
--   * cotr_area: the places the drivers sell or stand by (a CFD, an office park …): name, full address, coordinates.
--     Apart from the pools (where the fleet and the stock are kept); a roster slot sends a driver to an area.
--   * cotr_accessory: the accessories of the fleet (umbrella, cooler box, banner, power bank …), assigned to a fleet
--     (cotr_fleet_accessory, with the days it was put on / taken off); every shift keeps what its fleet carried
--     (cotr_shift_accessory), so who took what and when is known.
--   * cotr_schedule_slot: several assignments of a driver in one day (07:00–10:00 area A, 10:00–14:00 area B …), each from
--     a shift template with its own times; the day row (cotr_schedule) keeps the status and the first template.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_area (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    address     text NOT NULL DEFAULT '',
    district    text NOT NULL DEFAULT '',
    city        text NOT NULL DEFAULT '',
    province    text NOT NULL DEFAULT '',
    postal_code text NOT NULL DEFAULT '',
    latitude    numeric(9,6),
    longitude   numeric(9,6),
    radius_m    integer NOT NULL DEFAULT 100 CHECK (radius_m > 0),
    pool_code   text REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE SET NULL,  -- usually served from
    note        text,
    is_active   boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_accessory (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    category   text NOT NULL DEFAULT '',
    unit       text NOT NULL DEFAULT 'pcs',
    serial_no  text,
    condition  text NOT NULL DEFAULT 'good' CHECK (condition IN ('good', 'service', 'broken', 'lost')),
    note       text,
    is_active  boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet_accessory (
    id             bigserial PRIMARY KEY,
    fleet_code     text NOT NULL REFERENCES integration_portal.cotr_fleet (code) ON UPDATE CASCADE ON DELETE CASCADE,
    accessory_code text NOT NULL REFERENCES integration_portal.cotr_accessory (code) ON UPDATE CASCADE ON DELETE CASCADE,
    qty            integer NOT NULL DEFAULT 1 CHECK (qty > 0),
    assigned_on    date NOT NULL DEFAULT CURRENT_DATE,
    removed_on     date,                                   -- null = still on the fleet
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     uuid
);
CREATE UNIQUE INDEX IF NOT EXISTS cotr_fleet_accessory_on ON integration_portal.cotr_fleet_accessory (fleet_code, accessory_code) WHERE removed_on IS NULL;

CREATE TABLE IF NOT EXISTS integration_portal.cotr_shift_accessory (
    shift_id       uuid NOT NULL REFERENCES integration_portal.cotr_shift (id) ON DELETE CASCADE,
    accessory_code text NOT NULL REFERENCES integration_portal.cotr_accessory (code) ON UPDATE CASCADE ON DELETE CASCADE,
    fleet_code     text,
    qty            integer NOT NULL DEFAULT 1,
    PRIMARY KEY (shift_id, accessory_code)
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_schedule_slot (
    id            bigserial PRIMARY KEY,
    driver_code   text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day           date NOT NULL,
    template_code text REFERENCES integration_portal.cotr_shift_template (code) ON UPDATE CASCADE,
    start_time    time NOT NULL,
    end_time      time NOT NULL,
    area_code     text REFERENCES integration_portal.cotr_area (code) ON UPDATE CASCADE ON DELETE SET NULL,
    fleet_code    text REFERENCES integration_portal.cotr_fleet (code) ON UPDATE CASCADE ON DELETE SET NULL,
    note          text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid
);
CREATE INDEX IF NOT EXISTS cotr_schedule_slot_day ON integration_portal.cotr_schedule_slot (day, driver_code, start_time);
