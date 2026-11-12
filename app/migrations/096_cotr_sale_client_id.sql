-- 096: a sale recorded on the phone without signal and sent later (recommendation #19, 2026-11-12): the phone's own id
-- of the sale makes a resend harmless, and the time it was made is kept (not the time it reached the server).
ALTER TABLE integration_portal.cotr_sale ADD COLUMN IF NOT EXISTS client_id text;
CREATE UNIQUE INDEX IF NOT EXISTS cotr_sale_client_id ON integration_portal.cotr_sale (client_id) WHERE client_id IS NOT NULL;
