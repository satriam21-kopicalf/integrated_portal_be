-- 163: a cost line of Maintenance / Procurement may come from the material catalog of Building & Renovation (2026-11-27):
-- its estimate is the catalog price, and its actual price per unit goes to the price history of the material (source
-- INVOICE) for the Building team; the catalog price itself is changed by the team only.
ALTER TABLE integration_portal.de_cost_item ADD COLUMN IF NOT EXISTS material_id integer
    REFERENCES integration_portal.building_material(id) ON DELETE SET NULL;
ALTER TABLE integration_portal.building_price ADD COLUMN IF NOT EXISTS de_cost_item_id integer;
CREATE UNIQUE INDEX IF NOT EXISTS building_price_de_cost ON integration_portal.building_price (de_cost_item_id) WHERE de_cost_item_id IS NOT NULL;
