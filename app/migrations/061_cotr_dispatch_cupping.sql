-- 061 (2026-11-03): the daily assignment of the storage per driver keeps the cups for tasting apart: `lines` stays what
-- the driver carries (to sell + for cupping, the start stock), `cupping` the part of it given out for tasting
-- ({menu code: cups}); the storage sees it against the cupping counted at the end of the shift.
ALTER TABLE integration_portal.cotr_dispatch ADD COLUMN IF NOT EXISTS cupping jsonb NOT NULL DEFAULT '{}';
