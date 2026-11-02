-- 053 (2026-11-02): incentive schemes of Calf On The Road. A scheme is versioned by its start month; the monthly cups
-- of a driver (sales not voided) are paid per segment of the tiers (the default of the sheet "COTR - Oktober": 1500–2000
-- cups × Rp 500, 2000–3000 × 1000, 3000–4500 × 1500, above 4500 × 2000, below the first threshold nothing), plus a bonus
-- per cup of chosen menus, minus the deductions of the attendance of Master Schedule (late, absent), and only when the
-- attendance reaches the minimum. The COTR team adjusts the scheme; the month takes the latest scheme started by then.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_incentive_scheme (
    code          text PRIMARY KEY,
    name          text NOT NULL,
    valid_from    date NOT NULL,                       -- first day of the month it applies from
    tiers         jsonb NOT NULL DEFAULT '[]',         -- [{"from": cups, "to": cups | null, "rate": Rp per cup}]
    product_bonus jsonb NOT NULL DEFAULT '[]',         -- [{"menu": code, "rate": Rp per cup}]
    late_penalty  numeric(14,2) NOT NULL DEFAULT 0,    -- Rp per late day
    absent_penalty numeric(14,2) NOT NULL DEFAULT 0,   -- Rp per absent day
    min_attendance_pct numeric(5,2) NOT NULL DEFAULT 0, -- worked / scheduled below this: no incentive
    is_active     boolean NOT NULL DEFAULT true,
    note          text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid
);

INSERT INTO integration_portal.cotr_incentive_scheme (code, name, valid_from, tiers, note) VALUES
    ('PROGRESIF-2026', 'Progresif per cup (default)', '2026-01-01',
     '[{"from": 1500, "to": 2000, "rate": 500}, {"from": 2000, "to": 3000, "rate": 1000},
       {"from": 3000, "to": 4500, "rate": 1500}, {"from": 4500, "to": null, "rate": 2000}]',
     'Skema default dari sheet COTR - Oktober; cup per segmen di atas ambang')
ON CONFLICT (code) DO NOTHING;

-- the page of the incentives: the COTR management (salary & bank) sees them by default
INSERT INTO integration_portal.role_page (role, page)
SELECT role, 'cotr.incentive' FROM integration_portal.role_page WHERE page = 'cotr.management'
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'cotr.incentive', allowed FROM integration_portal.user_page WHERE page = 'cotr.management'
ON CONFLICT DO NOTHING;
