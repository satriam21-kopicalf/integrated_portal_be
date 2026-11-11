-- 088: Calf On The Road (2026-11-11).
--   * cotr_area_request: a driver asks to go to an area — one of Master → Area, or a new place found on the road (the name,
--     the address and the GPS of the phone there). The COTR team approves it (a new place becomes a master area) and the
--     roster of that day sends the driver there for the times asked; or rejects it with a reason.
--   * the waste of a pool / HUB is also kept per menu (cups of a batch expired, spilled …), and the cupping (cups given for
--     tasting at the pool / HUB) is recorded the same way: cotr_waste.kind 'cupping', cotr_waste.menu_code.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_area_request (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_code   text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day           date NOT NULL,
    area_code     text REFERENCES integration_portal.cotr_area (code) ON UPDATE CASCADE ON DELETE SET NULL,  -- picked, or made on approval
    name          text,                                   -- a new place: what the driver calls it
    address       text,
    city          text,
    latitude      numeric(9,6),
    longitude     numeric(9,6),
    accuracy_m    numeric(10,1),
    start_time    time,
    end_time      time,
    note          text,
    status        text NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'approved', 'rejected')),
    decided_by    uuid,
    decided_at    timestamptz,
    decision_note text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (area_code IS NOT NULL OR name IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS cotr_area_request_day ON integration_portal.cotr_area_request (day DESC, status);
CREATE INDEX IF NOT EXISTS cotr_area_request_driver ON integration_portal.cotr_area_request (driver_code, created_at DESC);

ALTER TABLE integration_portal.cotr_waste DROP CONSTRAINT IF EXISTS cotr_waste_kind_check;
ALTER TABLE integration_portal.cotr_waste ADD CONSTRAINT cotr_waste_kind_check CHECK (kind IN ('storage', 'production', 'cupping'));
ALTER TABLE integration_portal.cotr_waste ADD COLUMN IF NOT EXISTS menu_code text REFERENCES integration_portal.cotr_menu (code) ON UPDATE CASCADE ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS cotr_waste_pool_day ON integration_portal.cotr_waste (pool_code, day);

-- cotr_fleet_issue: a driver reports a maintenance need / issue of the armada (brake, battery, tyre, the cooler …) with a
-- photo and where it happened; the COTR management handles it: in progress (who handles it), resolved (what was done, the
-- cost) or rejected with a reason. Every step is kept in cotr_fleet_issue_log.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet_issue (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fleet_code      text NOT NULL REFERENCES integration_portal.cotr_fleet (code) ON UPDATE CASCADE ON DELETE CASCADE,
    driver_code     text REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE SET NULL,
    shift_id        uuid REFERENCES integration_portal.cotr_shift (id) ON DELETE SET NULL,
    category        text NOT NULL,
    severity        text NOT NULL DEFAULT 'medium' CHECK (severity IN ('low', 'medium', 'high')),  -- high: cannot sell
    title           text NOT NULL,
    description     text NOT NULL DEFAULT '',
    photo           bytea,
    photo_type      text,
    latitude        numeric(9,6),
    longitude       numeric(9,6),
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved', 'rejected')),
    handler         text,                                  -- who handles it (a workshop, a technician)
    resolution_note text,
    cost            numeric(14,2),
    resolved_at     timestamptz,
    updated_by      uuid,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_fleet_issue_status ON integration_portal.cotr_fleet_issue (status, created_at DESC);
CREATE INDEX IF NOT EXISTS cotr_fleet_issue_fleet ON integration_portal.cotr_fleet_issue (fleet_code, created_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet_issue_log (
    id         bigserial PRIMARY KEY,
    issue_id   uuid NOT NULL REFERENCES integration_portal.cotr_fleet_issue (id) ON DELETE CASCADE,
    status     text NOT NULL,
    note       text,
    by_name    text,
    by_user    uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_fleet_issue_log_issue ON integration_portal.cotr_fleet_issue_log (issue_id, created_at);
