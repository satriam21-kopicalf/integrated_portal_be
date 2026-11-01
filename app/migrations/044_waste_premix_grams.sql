-- 044: a premix / WIP waste is entered like the stock opname WIP (2026-11-01): in its count unit (Gram, Porsi, Pcs …),
-- its grams and the grams of every ingredient (recipe, rounded up per ingredient) are kept with the entry.
ALTER TABLE integration_portal.waste_entry ADD COLUMN IF NOT EXISTS qty_base numeric(14,3);
ALTER TABLE integration_portal.waste_entry ADD COLUMN IF NOT EXISTS exploded jsonb;
