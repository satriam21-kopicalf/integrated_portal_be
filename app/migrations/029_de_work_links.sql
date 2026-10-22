-- 029: Damaged Equipment (2026-10-22): the photo link of a technician. While a request is in progress the team sends the
-- technician a link (no sign-in, valid 1 hour); the technician takes 1–5 photos of the work done and sends them. The photos
-- land with the request as AFTER photos (uploaded by the technician, not a portal user) and the team then processes Done.

CREATE TABLE IF NOT EXISTS integration_portal.de_work_link (
    id               serial        PRIMARY KEY,
    token            text          NOT NULL UNIQUE,
    request_id       uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    technician_id    integer       REFERENCES integration_portal.de_technician (id) ON DELETE SET NULL,
    technician_name  text          NOT NULL,
    created_by       uuid,
    created_by_name  text,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    expires_at       timestamptz   NOT NULL,
    submitted_at     timestamptz,
    submit_note      text,
    revoked_at       timestamptz
);
CREATE INDEX IF NOT EXISTS de_work_link_request ON integration_portal.de_work_link (request_id, created_at);

-- photos sent through a link have no portal user
ALTER TABLE integration_portal.de_media ALTER COLUMN uploaded_by DROP NOT NULL;
ALTER TABLE integration_portal.de_media ADD COLUMN IF NOT EXISTS work_link_id integer
    REFERENCES integration_portal.de_work_link (id) ON DELETE SET NULL;

-- timeline: WORK = photo link sent to the technician / photos sent by the technician
ALTER TABLE integration_portal.de_event DROP CONSTRAINT IF EXISTS de_event_kind_check;
ALTER TABLE integration_portal.de_event ADD CONSTRAINT de_event_kind_check
    CHECK (kind IN ('SUBMIT', 'STATUS', 'ASSIGN', 'RELEASE', 'SWAP', 'COST', 'NOTE', 'DUPLICATE', 'WORK'));
