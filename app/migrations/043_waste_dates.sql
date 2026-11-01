-- 043: production and expired date of every wasted item (2026-11-01, user request), entered per SKU on the waste form.
ALTER TABLE integration_portal.waste_entry ADD COLUMN IF NOT EXISTS production_date date;
ALTER TABLE integration_portal.waste_entry ADD COLUMN IF NOT EXISTS expired_date date;
