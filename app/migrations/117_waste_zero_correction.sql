-- 117: a waste entry may be corrected to 0 by Cost Control (2026-11-17, Cost Control request): the attached photo shows
-- nothing (or another item) was wasted. The outlet still enters more than 0 (app/waste.py _qty); only the check of the
-- table allows 0 now. No data changes.
ALTER TABLE integration_portal.waste_entry DROP CONSTRAINT IF EXISTS waste_entry_qty_check;
ALTER TABLE integration_portal.waste_entry ADD CONSTRAINT waste_entry_qty_check CHECK (qty >= 0);
