-- 092: Calf On The Road pay (the request of the COTR team, 2026-11-12): beside the incentive of a month, a bonus and the
-- deductions split as their sheet — kasbon (cash advance), minus (shortage of stock / cash), warning — and others, each
-- entered with its note; and the payslip of every employee, shown in the COTR app once the month is published.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pay_adjustment (
    id          bigserial PRIMARY KEY,
    driver_code text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    month       date NOT NULL CHECK (extract(day FROM month) = 1),
    kind        text NOT NULL CHECK (kind IN ('bonus', 'kasbon', 'minus', 'warning', 'other')),
    amount      numeric(14,2) NOT NULL CHECK (amount > 0),
    note        text,
    source      text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'sheet')),  -- sheet: the Rekap Minus of a pool workbook
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_pay_adjustment_month ON integration_portal.cotr_pay_adjustment (month, driver_code);

-- a month whose payslips the employees see in the app (published by the COTR management)
CREATE TABLE IF NOT EXISTS integration_portal.cotr_payslip_month (
    month        date PRIMARY KEY CHECK (extract(day FROM month) = 1),
    published_at timestamptz NOT NULL DEFAULT now(),
    published_by uuid
);
