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
-- Every request and category is kept first in *_bak_20261116.

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

-- Merchandise is bought, never repaired: it moves to Procurement under the group the form's ESB picker uses
UPDATE integration_portal.de_category SET kind = 'PROCUREMENT', grp = 'Merchandise' WHERE name = 'Merchandise';

-- Procurement's own categories (copies of the repair ones that are also bought)
INSERT INTO integration_portal.de_category (kind, grp, name, default_urgency, examples, sort, is_active, branch_groups)
SELECT 'PROCUREMENT', c.grp, c.name, 'PLANNED', c.examples, c.sort, true, c.branch_groups
  FROM integration_portal.de_category c
 WHERE c.kind = 'REPAIR' AND c.name IN ('Heavy Tools', 'Light Tools', 'Mesin Kopi & Grinder', 'Refrigerasi', 'Furniture',
                                         'Neon Box & Signage', 'Dekorasi & Tanaman', 'Sticker & Branding', 'CCTV & Keamanan',
                                         'IT, POS & Jaringan', 'Renovasi & Desain')
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.de_category (kind, grp, name, default_urgency, examples, sort, is_active)
VALUES ('PROCUREMENT', 'Proyek & Lainnya', 'Lainnya', 'PLANNED', '{}', 999, true)
ON CONFLICT DO NOTHING;

-- items of procurement requests point to the procurement category of the same name
UPDATE integration_portal.de_item i SET category_id = p.id
  FROM integration_portal.de_request r, integration_portal.de_category c, integration_portal.de_category p
 WHERE r.id = i.request_id AND r.kind = 'PROCUREMENT' AND c.id = i.category_id AND c.kind = 'REPAIR'
   AND p.kind = 'PROCUREMENT' AND lower(p.name) = lower(c.name);

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
