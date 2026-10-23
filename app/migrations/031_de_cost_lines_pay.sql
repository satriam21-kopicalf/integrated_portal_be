-- 031: Damaged Equipment (2026-10-23):
--  * categories per branch group (CCI, CTG, CTGP …): empty = every branch;
--  * technician pay per hour, day, week, month or per job (borongan); the labour of a request accumulates from the time
--    a technician works on it, adjustable per assignment (with a note);
--  * cost lines of a request (material, service, transport, other) with a description, an estimate and an actual
--    amount; the request's estimated / actual cost are their totals (+ the labour).

ALTER TABLE integration_portal.de_category ADD COLUMN IF NOT EXISTS branch_groups text[] NOT NULL DEFAULT '{}';

ALTER TABLE integration_portal.de_technician DROP CONSTRAINT IF EXISTS de_technician_wage_type_check;
ALTER TABLE integration_portal.de_technician ADD CONSTRAINT de_technician_wage_type_check
    CHECK (wage_type IN ('HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', 'PER_JOB'));

ALTER TABLE integration_portal.de_assignment
    ADD COLUMN IF NOT EXISTS labor_adjust numeric(14, 2) CHECK (labor_adjust IS NULL OR labor_adjust >= 0),
    ADD COLUMN IF NOT EXISTS labor_note   text;

CREATE TABLE IF NOT EXISTS integration_portal.de_cost_item (
    id             serial        PRIMARY KEY,
    request_id     uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    kind           text          NOT NULL DEFAULT 'MATERIAL' CHECK (kind IN ('MATERIAL', 'SERVICE', 'TRANSPORT', 'OTHER')),
    description    text          NOT NULL,
    qty            numeric(12, 2) NOT NULL DEFAULT 1 CHECK (qty > 0),
    est_amount     numeric(14, 2) CHECK (est_amount IS NULL OR est_amount >= 0),     -- total of the line
    actual_amount  numeric(14, 2) CHECK (actual_amount IS NULL OR actual_amount >= 0),
    created_by     text,
    created_at     timestamptz   NOT NULL DEFAULT now(),
    updated_at     timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS de_cost_item_request ON integration_portal.de_cost_item (request_id, id);
