-- 030: Damaged Equipment (2026-10-22):
--  * target time adjustable by the maintenance team (due date & time with a reason; the PIC sees it);
--  * the handling mode follows the technicians: INTERNAL or VENDOR (never both at once);
--  * a new step HANDOVER ("Serah terima") between In progress and Done: the PIC confirms at the outlet with 1-5 camera
--    photos, their signature and — for a vendor — the vendor's signature; the team then processes Done;
--  * PIC photos while the work runs (MONITOR) and at the handover (HANDOVER).

ALTER TABLE integration_portal.de_request DROP CONSTRAINT IF EXISTS de_request_status_check;
ALTER TABLE integration_portal.de_request ADD CONSTRAINT de_request_status_check CHECK (status IN
    ('DRAFT', 'PENDING', 'VALIDATION', 'REVIEW', 'ON_HOLD', 'ACCEPTED', 'IN_PROGRESS', 'HANDOVER', 'DONE', 'REJECTED'));

ALTER TABLE integration_portal.de_request
    ADD COLUMN IF NOT EXISTS target_at             timestamptz,   -- adjusted due time (NULL = from the urgency)
    ADD COLUMN IF NOT EXISTS target_note           text,
    ADD COLUMN IF NOT EXISTS target_by             text,
    ADD COLUMN IF NOT EXISTS target_set_at         timestamptz,
    ADD COLUMN IF NOT EXISTS target_changes        integer       NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS mode                  text          CHECK (mode IN ('INTERNAL', 'VENDOR')),
    ADD COLUMN IF NOT EXISTS handover_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS handover_at           timestamptz,
    ADD COLUMN IF NOT EXISTS handover_note         text,
    ADD COLUMN IF NOT EXISTS handover_latitude     numeric(9, 6),
    ADD COLUMN IF NOT EXISTS handover_longitude    numeric(9, 6),
    ADD COLUMN IF NOT EXISTS handover_distance_m   integer,
    ADD COLUMN IF NOT EXISTS handover_outside      boolean       NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pic_sign              bytea,
    ADD COLUMN IF NOT EXISTS pic_sign_type         text,
    ADD COLUMN IF NOT EXISTS pic_sign_name         text,
    ADD COLUMN IF NOT EXISTS vendor_sign           bytea,
    ADD COLUMN IF NOT EXISTS vendor_sign_type      text,
    ADD COLUMN IF NOT EXISTS vendor_sign_name      text,
    ADD COLUMN IF NOT EXISTS approved_by           text;          -- who processed Done

ALTER TABLE integration_portal.de_media DROP CONSTRAINT IF EXISTS de_media_kind_check;
ALTER TABLE integration_portal.de_media ADD CONSTRAINT de_media_kind_check
    CHECK (kind IN ('PHOTO', 'VIDEO', 'AFTER', 'INVOICE', 'MONITOR', 'HANDOVER'));

ALTER TABLE integration_portal.de_event DROP CONSTRAINT IF EXISTS de_event_kind_check;
ALTER TABLE integration_portal.de_event ADD CONSTRAINT de_event_kind_check
    CHECK (kind IN ('SUBMIT', 'STATUS', 'ASSIGN', 'RELEASE', 'SWAP', 'COST', 'NOTE', 'DUPLICATE', 'WORK', 'TARGET', 'HANDOVER'));
