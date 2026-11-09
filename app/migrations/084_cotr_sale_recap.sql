-- 084: Calf On The Road (2026-11-09): the sales of a shift may be recorded two ways —
--   * live: every transaction by the driver in the app (as before);
--   * recap: back at the pool, the cups sold that were not tapped in are worked out from the stock — what the fleet
--     carried − what is left − waste − cupping — and booked at the end of the shift as recap sales at the menu price,
--     the QRIS part as the driver reads it from the QRIS mutations, the rest cash. Both may be mixed in one shift.
ALTER TABLE integration_portal.cotr_sale ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'live';
ALTER TABLE integration_portal.cotr_sale DROP CONSTRAINT IF EXISTS cotr_sale_source_check;
ALTER TABLE integration_portal.cotr_sale ADD CONSTRAINT cotr_sale_source_check CHECK (source IN ('live', 'recap'));
