-- 060 (2026-11-03): Calf On The Road on the sample "DATA COTR - SELIS ARMADA" (fe-calfontheroad/docs, not committed:
-- personal data). The team is every employee of COTR (Armada = the drivers, Storage, Stockist, Pool Leader, Officer,
-- Central Kitchen …) keyed by the employee id (CALF…), with the nickname the reports use and the bike (S1…S44); the
-- pools of the sample; the attendance codes of the payroll sheets as shift templates with their day rate (N = normal,
-- H = half, LS = long shift; rates read from the GAPOK of the sheets: 85.000 / 75.000 / 150.000, period 26 → 25).
-- The team itself is loaded by the import of Master Driver (no personal data in the repository).
ALTER TABLE integration_portal.cotr_driver
    ADD COLUMN IF NOT EXISTS employee_id text,
    ADD COLUMN IF NOT EXISTS nickname text,
    ADD COLUMN IF NOT EXISTS department text,
    ADD COLUMN IF NOT EXISTS bike_code text;
CREATE UNIQUE INDEX IF NOT EXISTS cotr_driver_employee ON integration_portal.cotr_driver (upper(employee_id)) WHERE employee_id IS NOT NULL;

INSERT INTO integration_portal.cotr_pool (code, name, kind) VALUES
    ('POOL-TBT', 'Pool Tebet', 'main'),
    ('POOL-PLM', 'Pool Palmerah', 'main'),
    ('POOL-PSM', 'Pool Pasar Minggu', 'main'),
    ('POOL-CPT', 'Pool Cipete', 'main'),
    ('POOL-RTN', 'Ratna (Central Kitchen)', 'storage')
ON CONFLICT (code) DO NOTHING;

ALTER TABLE integration_portal.cotr_shift_template ADD COLUMN IF NOT EXISTS pay_rate numeric(14,2) CHECK (pay_rate >= 0);
INSERT INTO integration_portal.cotr_shift_template (code, name, start_time, end_time, break_min, grace_min, color, sort, pay_rate) VALUES
    ('N', 'Normal', '07:00', '15:00', 60, 15, 'blue', 10, 85000),
    ('H', 'Half', '07:00', '12:00', 0, 15, 'amber', 11, 75000),
    ('LS', 'Long Shift', '07:00', '19:00', 60, 15, 'violet', 12, 150000)
ON CONFLICT (code) DO NOTHING;
