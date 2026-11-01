-- 047 (2026-11-01): COTR as it runs today, from the sheet "COTR - Oktober" (fe-calfontheroad/docs): pool Kwitang
-- (Jakarta), the 46 armada named after their QRIS account (BNI 01, BRI 05 …) and the six menus with their prices; it
-- replaces the masters of the prototype (046) while no shift has been recorded on them. The drivers (people) are not
-- kept in the repository: the COTR team enters them in Monitoring COTR → Master Driver.
--
-- The end of a shift follows the "Report Armada" sheet: per menu the cups left (S), wasted (W) and given for tasting
-- (C, cupping), so sold = taken - left - cupping - waste; the cash counted (ACTUAL CASH) and the cash spent on the road
-- from it (OPERASIONAL) give the balance of the day (- short / + over, the "Rekap Minus" sheet).
ALTER TABLE integration_portal.cotr_shift ADD COLUMN IF NOT EXISTS end_waste jsonb;
ALTER TABLE integration_portal.cotr_shift ADD COLUMN IF NOT EXISTS end_cupping jsonb;
ALTER TABLE integration_portal.cotr_shift ADD COLUMN IF NOT EXISTS cash_actual numeric(14,2);
ALTER TABLE integration_portal.cotr_shift ADD COLUMN IF NOT EXISTS cash_spent numeric(14,2);

-- costs a driver asks back: the categories of the "Pengeluaran" sheet that happen on the road (+ stall rent)
ALTER TABLE integration_portal.cotr_request DROP CONSTRAINT IF EXISTS cotr_request_kind_check;
ALTER TABLE integration_portal.cotr_request
    ADD CONSTRAINT cotr_request_kind_check CHECK (kind IN ('fuel', 'ice', 'stall', 'repair', 'parking', 'other'));

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM integration_portal.cotr_shift) THEN
        DELETE FROM integration_portal.cotr_driver WHERE code IN ('DRV-001', 'DRV-002', 'DRV-003', 'DRV-004', 'DRV-005');
        DELETE FROM integration_portal.cotr_fleet WHERE code IN ('COTR-M01', 'COTR-M02', 'COTR-M03', 'COTR-G01', 'COTR-S01');
        DELETE FROM integration_portal.cotr_menu WHERE code IN ('COTR-EKC', 'COTR-AMR', 'COTR-ECH', 'COTR-MTC');
        DELETE FROM integration_portal.cotr_pool WHERE code IN ('STR-BDG-01', 'POOL-DAGO', 'POOL-BUAH', 'HUB-BDG');
        DELETE FROM integration_portal.cotr_bom WHERE code IN ('BOM-EKC', 'BOM-PRM-EKC', 'BOM-ECH', 'BOM-AMR');
    END IF;
END $$;

INSERT INTO integration_portal.cotr_pool (code, name, kind, parent_code, city, address, storage) VALUES
    ('POOL-KWI', 'Pool Kwitang', 'main', NULL, 'Jakarta Pusat', 'Kwitang', '{Menu jadi,Es batu,Kemasan,Tools armada}')
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.cotr_menu (code, name, category, price, bom_code, is_active, sort) VALUES
    ('COTR-PRM-CUP', 'Calf Premium Cup', 'Kopi susu', 15000, NULL, true, 1),
    ('COTR-PRM-CAN', 'Premium Can 250', 'Kopi susu', 23000, NULL, true, 2),
    ('COTR-CHO-CAN', 'Chocolate Can 250', 'Non kopi', 25000, NULL, true, 3),
    ('COTR-PEC-CAN', 'Smooth Pecan 250', 'Kopi susu', 25000, NULL, true, 4),
    ('COTR-MTC-CUP', 'Matcha Latte', 'Non kopi', 18000, NULL, true, 5),
    ('COTR-BAN-CAN', 'Smooth Banana 250', 'Kopi susu', 25000, NULL, true, 6)
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.cotr_fleet (code, type, plate, pool_code, capacity, condition) VALUES
    ('BNI 01', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 02', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 19', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 23', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 34', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 37', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 38', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 40', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 42', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 44', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 46', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 49', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 53', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 58', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 63', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 64', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 68', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 70', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 76', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 79', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 80', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 83', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 84', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 97', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 116', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 141', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 134', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 33', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 73', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BNI 122', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 05', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 08', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 09', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 11', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 12', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 18', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 28', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 35', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 39', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 44', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 22', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 19', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 32', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 24', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 33', 'sepeda', NULL, 'POOL-KWI', 0, 'good'),
    ('BRI 56', 'sepeda', NULL, 'POOL-KWI', 0, 'good')
ON CONFLICT (code) DO NOTHING;

