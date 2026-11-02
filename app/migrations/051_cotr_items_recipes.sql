-- 051 (2026-11-02): items and recipe cards of Calf On The Road, on the standard of the premix calculator of Cost
-- Control ("Kalkulator Premix WIP - SO BAR KITCHEN": a premix is a batch in grams of raw materials, taken by unit —
-- Pail, Teko, Gram — and the calculator rounds every material up). Kept in Monitoring COTR so the COTR team adjusts
-- its own: cotr_item = what COTR keeps (raw materials, premix, packaging, supplies, tools; unit, pack, cost);
-- cotr_bom.kind = 'premix' (a batch of a premix item, its units) or 'menu' (the recipe card of one cup / can: premix,
-- packaging, ice). Sales, waste and cupping of the drivers are exploded through the cards into the theoretical usage.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_item (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    category   text NOT NULL CHECK (category IN ('raw', 'premix', 'packaging', 'supply', 'tools', 'other')),
    unit       text NOT NULL,                              -- base unit: g, ml, pcs
    pack_unit  text,                                       -- how it is bought / counted: Pack, Dus, Pail …
    pack_qty   numeric(14,3) CHECK (pack_qty > 0),          -- base units in one pack
    cost       numeric(14,4) CHECK (cost >= 0),            -- per base unit (HPP of the recipe cards)
    is_stock   boolean NOT NULL DEFAULT true,               -- false: water and the like, not counted
    is_active  boolean NOT NULL DEFAULT true,
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

ALTER TABLE integration_portal.cotr_bom
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'menu' CHECK (kind IN ('premix', 'menu')),
    ADD COLUMN IF NOT EXISTS units jsonb NOT NULL DEFAULT '[]',   -- [{name, grams}] the containers of a premix
    ADD COLUMN IF NOT EXISTS note text;

-- items: the raw materials of the premix calculator, the packaging / supplies of the sheet "SO Barang Pool", the
-- tools of "Checker Inventory" (COTR - Oktober), the ice bought every day
INSERT INTO integration_portal.cotr_item (code, name, category, unit, pack_unit, pack_qty, is_stock) VALUES
    ('RM-FRESHMILK', 'Fresh Milk', 'raw', 'g', 'Pack 946 ml', 946, true),
    ('RM-ESP-PREMIUM', 'Espresso Premium', 'raw', 'g', NULL, NULL, true),
    ('RM-ESP-REGULAR', 'Espresso Regular', 'raw', 'g', NULL, NULL, true),
    ('RM-CREAMER', 'Creamer Bubuk', 'raw', 'g', 'Pack 500 g', 500, true),
    ('RM-EVAPORATED', 'FN Evaporated', 'raw', 'g', 'Kaleng 380 g', 380, true),
    ('RM-SKM', 'SKM', 'raw', 'g', 'Kaleng 365 g', 365, true),
    ('RM-JURUS', 'Jurus', 'raw', 'g', 'Pack 1000 g', 1000, true),
    ('RM-COKLAT', 'Powder Coklat', 'raw', 'g', 'Pack 1000 g', 1000, true),
    ('RM-MATCHA', 'Matcha Powder', 'raw', 'g', 'Pack 760 g', 760, true),
    ('RM-AREN', 'Gula Aren', 'raw', 'g', 'Pack 1300 g', 1300, true),
    ('RM-AIR', 'Air', 'raw', 'g', NULL, NULL, false),
    ('RM-ESBATU', 'Es Batu', 'raw', 'g', 'Karung', NULL, true),
    ('PRM-PREMIUM', 'Premix Premium (NEW)', 'premix', 'g', 'Pail', 6615, true),
    ('PRM-REGULAR', 'Premix Regular', 'premix', 'g', 'Pail', 6975, true),
    ('PRM-SMOOTH', 'Premix Smooth', 'premix', 'g', 'Pail', 5500, true),
    ('PRM-CHOCOLATE', 'Premix Chocolate', 'premix', 'g', NULL, NULL, true),
    ('PRM-MATCHA', 'Premix Matcha', 'premix', 'g', NULL, NULL, true),
    ('PK-SEDOTAN-5MM', 'Sedotan 5 mm', 'packaging', 'pack', 'Dus (20 Pack)', 20, true),
    ('PK-TUTUP-CUP-ES', 'Tutup Cup Es', 'packaging', 'pack', 'Dus (20 Pack)', 20, true),
    ('PK-CUP-10OZ', 'Cup 10 oz (Gemoy)', 'packaging', 'pack', 'Dus (20 Pack)', 20, true),
    ('PK-KALENG-250', 'Kaleng 250 ml', 'packaging', 'pcs', NULL, NULL, true),
    ('PK-KLIP-20X12', 'Plastik Klip 20x12 cm', 'packaging', 'pack', 'Karung (125 Pack)', 125, true),
    ('PK-TA-17', 'Plastik Take Away 17', 'packaging', 'pack', 'Karung (25 Pack)', 25, true),
    ('PK-TA-24', 'Plastik Take Away 24', 'packaging', 'pack', 'Karung (25 Pack)', 25, true),
    ('PK-TA-28', 'Plastik Take Away 28', 'packaging', 'pack', 'Karung (25 Pack)', 25, true),
    ('PK-TA-35', 'Plastik Take Away 35', 'packaging', 'pack', 'Karung (25 Pack)', 25, true),
    ('SP-TISSUE-LIVI', 'Tissue Livi', 'supply', 'pack', 'Dus (24 Pack)', 24, true),
    ('SP-HAND-GLOVE', 'Hand Glove', 'supply', 'pack', 'Dus (10 Pack)', 10, true),
    ('SP-TRASH-BAG', 'Trash Bag Besar', 'supply', 'pack', NULL, NULL, true),
    ('TL-BATERAI', 'Baterai', 'tools', 'pcs', NULL, NULL, true),
    ('TL-PAYUNG', 'Payung', 'tools', 'pcs', NULL, NULL, true),
    ('TL-MENU', 'Menu', 'tools', 'pcs', NULL, NULL, true),
    ('TL-SCOOP-ICE', 'Scoop Ice', 'tools', 'pcs', NULL, NULL, true),
    ('TL-HELM', 'Helm', 'tools', 'pcs', NULL, NULL, true)
ON CONFLICT (code) DO NOTHING;

-- the premix of the calculator (grams per batch; Chocolate and Matcha come as ratios: a batch of 1000 g)
INSERT INTO integration_portal.cotr_bom (code, name, output, yield_qty, yield_unit, kind, units, lines) VALUES
    ('BOM-PRM-PREMIUM', 'Premix Premium (NEW)', 'PRM-PREMIUM', 6615, 'g', 'premix', '[{"name": "Pail (3 Teko)", "grams": 6615}, {"name": "Gram", "grams": 1}]',
     '[{"item": "RM-FRESHMILK", "qty": 2700, "unit": "g"}, {"item": "RM-ESP-PREMIUM", "qty": 1665, "unit": "g"}, {"item": "RM-CREAMER", "qty": 1125, "unit": "g"}, {"item": "RM-AIR", "qty": 1125, "unit": "g"}]'),
    ('BOM-PRM-REGULAR', 'Premix Regular', 'PRM-REGULAR', 6975, 'g', 'premix', '[{"name": "Pail (3 Teko)", "grams": 6975}, {"name": "Gram", "grams": 1}]',
     '[{"item": "RM-FRESHMILK", "qty": 3150, "unit": "g"}, {"item": "RM-ESP-REGULAR", "qty": 1575, "unit": "g"}, {"item": "RM-CREAMER", "qty": 1125, "unit": "g"}, {"item": "RM-AIR", "qty": 1125, "unit": "g"}]'),
    ('BOM-PRM-SMOOTH', 'Premix Smooth', 'PRM-SMOOTH', 5500, 'g', 'premix', '[{"name": "Pail", "grams": 5500}, {"name": "Gram", "grams": 1}]',
     '[{"item": "RM-FRESHMILK", "qty": 2400, "unit": "g"}, {"item": "RM-ESP-PREMIUM", "qty": 500, "unit": "g"}, {"item": "RM-EVAPORATED", "qty": 1200, "unit": "g"}, {"item": "RM-SKM", "qty": 200, "unit": "g"}, {"item": "RM-CREAMER", "qty": 600, "unit": "g"}, {"item": "RM-AIR", "qty": 600, "unit": "g"}]'),
    ('BOM-PRM-CHOCOLATE', 'Premix Chocolate', 'PRM-CHOCOLATE', 1000, 'g', 'premix', '[{"name": "Gram", "grams": 1}]',
     '[{"item": "RM-FRESHMILK", "qty": 576.271, "unit": "g"}, {"item": "RM-SKM", "qty": 101.695, "unit": "g"}, {"item": "RM-CREAMER", "qty": 67.797, "unit": "g"}, {"item": "RM-AIR", "qty": 101.695, "unit": "g"}, {"item": "RM-JURUS", "qty": 50.847, "unit": "g"}, {"item": "RM-COKLAT", "qty": 33.898, "unit": "g"}]'),
    ('BOM-PRM-MATCHA', 'Premix Matcha', 'PRM-MATCHA', 1000, 'g', 'premix', '[{"name": "Gram", "grams": 1}]',
     '[{"item": "RM-FRESHMILK", "qty": 465.116, "unit": "g"}, {"item": "RM-EVAPORATED", "qty": 232.558, "unit": "g"}, {"item": "RM-AIR", "qty": 186.047, "unit": "g"}, {"item": "RM-MATCHA", "qty": 93.023, "unit": "g"}, {"item": "RM-AREN", "qty": 23.256, "unit": "g"}]')
ON CONFLICT (code) DO NOTHING;
