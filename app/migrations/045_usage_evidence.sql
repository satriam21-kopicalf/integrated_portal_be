-- 045 (2026-11-01, user request): every Usage Management SKU gets a category, a reason and a photo as evidence, like
-- a waste entry; the header row "Category Waste" of the sample sheet is not a category.
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS note text;
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS photo bytea;
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS photo_type text;
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS photo_taken_at timestamptz;
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS latitude numeric(9,6);
ALTER TABLE integration_portal.usage_count ADD COLUMN IF NOT EXISTS longitude numeric(9,6);
DELETE FROM integration_portal.waste_category WHERE name = 'Category Waste';
