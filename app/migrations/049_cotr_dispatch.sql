-- 049 (2026-11-02): the storage team of a pool assigns what every driver takes. Before the start of a shift the
-- storage assigns the cups per menu of the day (cotr_dispatch); the driver checks them against what is on the fleet:
-- all there -> accepted, else the driver sends the counts found and a note (disputed) and the storage decides the final
-- cups (accepted). Only then the driver checks in, with a signature, and the start stock is the final assignment.
-- Reloads follow the same check: the storage approves the cups (maybe fewer than asked), the driver checks what came
-- (received) or reports a difference (disputed) that the storage settles; the stock grows by the final cups.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_dispatch (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    day             date NOT NULL,
    driver_code     text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    pool_code       text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    lines           jsonb NOT NULL,                       -- {menu code: cups} assigned by the storage
    note            text,
    status          text NOT NULL DEFAULT 'assigned' CHECK (status IN ('assigned', 'disputed', 'accepted')),
    assigned_at     timestamptz NOT NULL DEFAULT now(),
    assigned_by     uuid,
    driver_counts   jsonb,                                -- what the driver found on the fleet (a difference)
    driver_note     text,
    checked_at      timestamptz,
    final_lines     jsonb,                                -- the start stock of the shift
    resolved_at     timestamptz,
    resolved_by     uuid,
    resolution_note text,
    UNIQUE (day, driver_code)
);
CREATE INDEX IF NOT EXISTS cotr_dispatch_pool_day ON integration_portal.cotr_dispatch (pool_code, day);

ALTER TABLE integration_portal.cotr_shift ADD COLUMN IF NOT EXISTS dispatch_id uuid REFERENCES integration_portal.cotr_dispatch (id);
ALTER TABLE integration_portal.cotr_check_in ADD COLUMN IF NOT EXISTS signature bytea;
ALTER TABLE integration_portal.cotr_check_in ADD COLUMN IF NOT EXISTS signature_type text;

-- reloads: approved cups, the driver's check, the settlement of a difference
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS approved_lines jsonb;
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS driver_counts jsonb;
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS driver_note text;
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS received_lines jsonb;   -- what was added to the stock
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS resolved_at timestamptz;
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS resolved_by uuid;
ALTER TABLE integration_portal.cotr_reload ADD COLUMN IF NOT EXISTS resolution_note text;
ALTER TABLE integration_portal.cotr_reload DROP CONSTRAINT IF EXISTS cotr_reload_status_check;
ALTER TABLE integration_portal.cotr_reload ADD CONSTRAINT cotr_reload_status_check
    CHECK (status IN ('requested', 'approved', 'rejected', 'disputed', 'received'));
-- reloads received before 049 added what was asked
UPDATE integration_portal.cotr_reload SET received_lines = lines WHERE status = 'received' AND received_lines IS NULL;

-- the storage page of the team (assign, settle differences, approve reloads), given like every page
INSERT INTO integration_portal.role_page (role, page)
SELECT DISTINCT role, 'cotr.storage' FROM integration_portal.role_page WHERE page = 'cotr.check-in'
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'cotr.storage', allowed FROM integration_portal.user_page WHERE page = 'cotr.check-in'
ON CONFLICT DO NOTHING;
