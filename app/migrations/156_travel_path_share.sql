-- 156: Travel Path shared (2026-11-26): every report sent gets a link anyone may open (no sign-in) with its evaluation,
-- findings and photos, put in the message for the WhatsApp group (app/travel_path.py shared).
ALTER TABLE integration_portal.travel_path ADD COLUMN IF NOT EXISTS share_token text UNIQUE;
UPDATE integration_portal.travel_path SET share_token = replace(gen_random_uuid()::text, '-', '')
 WHERE status = 'SUBMITTED' AND share_token IS NULL;
