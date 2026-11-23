-- 134: Report Armada entered in the portal as the sheet of the pools (2026-11-23). The storage enters B / S / C / W per
-- armada (products), the office (page cotr.finance, "COTR Office", given by a superadmin per account) enters the QRIS
-- and the actual cash; operational = the driver requests the office approved that day; balance = actual cash − (cash
-- revenue − operational). A row entered in the portal (source 'portal') is the official day of that armada: it wins
-- over the driver app and the imported workbooks.
ALTER TABLE integration_portal.cotr_report_day ADD COLUMN IF NOT EXISTS products_by text;
ALTER TABLE integration_portal.cotr_report_day ADD COLUMN IF NOT EXISTS products_at timestamptz;
ALTER TABLE integration_portal.cotr_report_day ADD COLUMN IF NOT EXISTS money_by text;
ALTER TABLE integration_portal.cotr_report_day ADD COLUMN IF NOT EXISTS money_at timestamptz;
CREATE INDEX IF NOT EXISTS cotr_report_day_driver_day ON integration_portal.cotr_report_day (driver_code, day);
