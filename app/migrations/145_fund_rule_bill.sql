-- 145: Tagihan Outlet (BILL) does not ask a vendor's invoice number (2026-11-24): migration 144 switched it on while the
-- outlet form has no such field. Finance may switch it on in Configurations, Rules.
UPDATE integration_portal.fund_rule SET vendor = false WHERE key = 'BILL' AND updated_by IS NULL;
