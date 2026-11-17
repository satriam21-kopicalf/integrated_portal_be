-- 114: Maintenance and Procurement apart (2026-11-16, user request).
--  * every category belongs to one form: kind REPAIR (Maintenance) or PROCUREMENT; the same name may exist in both
--    (Heavy Tools to repair / to buy). Procurement gets its own categories; Heavy Tools lists the 19 machines of
--    fe-operations/docs/LIST HEAVY TOOLS.xlsx. Group names tidied (ACCESORIES, capitals); Merchandise gets the group
--    "Merchandise" the form looks for (its ESB merchandise picker never showed).
--  * shorter flows: Validation and Review become one approval step on Pending (the approval steps per kind of 038
--    now sit on Pending). Maintenance: Pending -> Accepted -> In progress -> Handover -> Done. Procurement: Pending ->
--    Accepted -> Ordered -> Handover (received by the PIC) -> Done.
--  * priority: the PIC gives the impact (Maintenance) or the reason (Procurement); with the branch's sales tier
--    (A top 20 % of 30-day sales, C bottom 20 %) it sets the urgency (app/damaged_equipment.py priority()).
-- Every request and category is kept first in *_bak_20261116. Then (user, 2026-11-17) Procurement holds only the LIST
-- HEAVY TOOLS machines and every request so far is reset (backups *_backup_20261117).

CREATE TABLE IF NOT EXISTS integration_portal.de_request_bak_20261116 AS
SELECT id, number, kind, status, hold_from, urgency, approval_step, now() AS saved_at FROM integration_portal.de_request;
CREATE TABLE IF NOT EXISTS integration_portal.de_category_bak_20261116 AS SELECT *, now() AS saved_at FROM integration_portal.de_category;
CREATE TABLE IF NOT EXISTS integration_portal.de_item_bak_20261116 AS SELECT id, request_id, category_id, now() AS saved_at FROM integration_portal.de_item;

-- categories per form
ALTER TABLE integration_portal.de_category ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'REPAIR';
ALTER TABLE integration_portal.de_category DROP CONSTRAINT IF EXISTS de_category_kind_check;
ALTER TABLE integration_portal.de_category ADD CONSTRAINT de_category_kind_check CHECK (kind IN ('REPAIR', 'PROCUREMENT'));
ALTER TABLE integration_portal.de_category DROP CONSTRAINT IF EXISTS de_category_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS de_category_kind_name ON integration_portal.de_category (kind, lower(name));

UPDATE integration_portal.de_category SET grp = CASE grp
    WHEN 'TOOLS & HEAVY TOOLS' THEN 'Tools & Heavy Tools' WHEN 'ACCESORIES' THEN 'Aksesoris' WHEN 'BANGUNAN / SIPIL' THEN 'Bangunan / Sipil'
    WHEN 'INTERIOR & LANDSCAPE' THEN 'Interior & Landscape' WHEN 'ARSITEKTUR' THEN 'Arsitektur' ELSE grp END;

-- the 19 machines of the heavy tools list, as examples to pick (repair and buy)
UPDATE integration_portal.de_category SET examples = ARRAY['Mesin Seal Eton', 'Freezer 310L', 'Freezer 210L', 'Showcase 1 Pintu',
    'Showcase 2 Pintu', 'Mesin Cannular', 'Undercounter XCF 180', 'Blender', 'Steamer Dimsum', 'PSU Mesin Kaleng', 'Mesin Slushy',
    'Deep Fryer 2 Tungku', 'Mini Showcase', 'Mesin Fruktos Gula Aren', 'Juice Dispenser 2 Tabung', 'Mesin Seal Getra',
    'Low Temperature Freezer', 'Mesin Slushy 3 Tungku', 'Mesin Fruktos Gula Aren Powerpack']
 WHERE kind = 'REPAIR' AND name = 'Heavy Tools';

-- Merchandise is bought, never repaired: it leaves Maintenance; Procurement holds only the heavy tools list (inactive,
-- a monitoring user can switch it on again in Categories)
UPDATE integration_portal.de_category SET kind = 'PROCUREMENT', grp = 'Merchandise', is_active = false WHERE name = 'Merchandise';

-- Procurement's categories = fe-operations/docs/LIST HEAVY TOOLS.xlsx (user, 2026-11-17): one category Heavy Tools,
-- the item is picked from its 19 machines (PCS)
INSERT INTO integration_portal.de_category (kind, grp, name, default_urgency, examples, sort, is_active)
VALUES ('PROCUREMENT', 'Heavy Tools', 'Heavy Tools', 'PLANNED', ARRAY['Mesin Seal Eton', 'Freezer 310L', 'Freezer 210L',
    'Showcase 1 Pintu', 'Showcase 2 Pintu', 'Mesin Cannular', 'Undercounter XCF 180', 'Blender', 'Steamer Dimsum',
    'PSU Mesin Kaleng', 'Mesin Slushy', 'Deep Fryer 2 Tungku', 'Mini Showcase', 'Mesin Fruktos Gula Aren',
    'Juice Dispenser 2 Tabung', 'Mesin Seal Getra', 'Low Temperature Freezer', 'Mesin Slushy 3 Tungku',
    'Mesin Fruktos Gula Aren Powerpack'], 1, true)
ON CONFLICT DO NOTHING;

-- reset (user, 2026-11-17): every Maintenance / Procurement request so far is removed, as 070. Every row is kept first
-- in a *_backup_20261117 table; photo files stay on the backend volume. Categories and technicians stay.
CREATE TABLE IF NOT EXISTS integration_portal.de_request_backup_20261117 AS SELECT * FROM integration_portal.de_request;
CREATE TABLE IF NOT EXISTS integration_portal.de_item_backup_20261117 AS SELECT * FROM integration_portal.de_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_media_backup_20261117 AS SELECT * FROM integration_portal.de_media;
CREATE TABLE IF NOT EXISTS integration_portal.de_event_backup_20261117 AS SELECT * FROM integration_portal.de_event;
CREATE TABLE IF NOT EXISTS integration_portal.de_assignment_backup_20261117 AS SELECT * FROM integration_portal.de_assignment;
CREATE TABLE IF NOT EXISTS integration_portal.de_work_link_backup_20261117 AS SELECT * FROM integration_portal.de_work_link;
CREATE TABLE IF NOT EXISTS integration_portal.de_cost_item_backup_20261117 AS SELECT * FROM integration_portal.de_cost_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_counter_backup_20261117 AS SELECT * FROM integration_portal.de_counter;
UPDATE integration_portal.de_request SET duplicate_of = NULL
    WHERE duplicate_of IN (SELECT id FROM integration_portal.de_request_backup_20261117);
DELETE FROM integration_portal.de_request r
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_request_backup_20261117 b WHERE b.id = r.id);
DELETE FROM integration_portal.de_counter c
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_counter_backup_20261117 b WHERE b.month = c.month);

-- impact / reason / sales tier, and the procurement steps
ALTER TABLE integration_portal.de_request ADD COLUMN IF NOT EXISTS impact text;       -- REPAIR: STOPS | DEGRADED | MINOR
ALTER TABLE integration_portal.de_request ADD COLUMN IF NOT EXISTS reason text;       -- PROCUREMENT: REPLACE | CAPACITY | OTHER
ALTER TABLE integration_portal.de_request ADD COLUMN IF NOT EXISTS branch_tier text;  -- A | B | C at the last save
ALTER TABLE integration_portal.de_request ADD COLUMN IF NOT EXISTS ordered_at timestamptz;

ALTER TABLE integration_portal.de_request DROP CONSTRAINT IF EXISTS de_request_status_check;
UPDATE integration_portal.de_request SET status = 'PENDING', approval_step = 1 WHERE status IN ('VALIDATION', 'REVIEW');
UPDATE integration_portal.de_request SET hold_from = 'PENDING' WHERE hold_from IN ('VALIDATION', 'REVIEW');
ALTER TABLE integration_portal.de_request ADD CONSTRAINT de_request_status_check CHECK (status IN
    ('DRAFT', 'PENDING', 'ON_HOLD', 'ACCEPTED', 'IN_PROGRESS', 'ORDERED', 'HANDOVER', 'DONE', 'REJECTED'));
