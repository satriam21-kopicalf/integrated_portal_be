-- 024: signature of a Monitoring Product / Outlet visit (2026-10-21, like the stock opname, D-62 of kopicalf): after a
-- preview of the visit the PIC may sign it before sending (optional); the image (JPEG on white) and the name written
-- under it are kept with the visit and shown in Monitoring.

ALTER TABLE integration_portal.visit_form
    ADD COLUMN IF NOT EXISTS signature       bytea,
    ADD COLUMN IF NOT EXISTS signature_type  text,
    ADD COLUMN IF NOT EXISTS signed_name     text,
    ADD COLUMN IF NOT EXISTS signed_at       timestamptz;
