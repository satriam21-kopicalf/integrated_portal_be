-- 101: Pusat Informasi of Cost Control (page cc.info, 2026-11-13; app/data_health.py): how complete and fresh the data
-- behind the COGS, the Usage Ratio and the Purchase Estimate is. Given to every role that opens a Cost Control page.
INSERT INTO integration_portal.role_page (role, page)
SELECT DISTINCT r.role, 'cc.info' FROM integration_portal.role_page r WHERE r.page LIKE 'cc.%'
ON CONFLICT DO NOTHING;
