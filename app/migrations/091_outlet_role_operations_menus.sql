-- 091: the outlet / HUB accounts (role outlet, Kopi Calf and Wasgee) open in Operations exactly: Stock Opname, Waste,
-- Transfer Stock, Damaged Equipment, Fund Request, Aggregator (GrabFood, Google Maps, ShopeeFood, GoFood) and the Guide
-- (2026-11-11). The exceptions per outlet account (Fund Request taken off, Surveyor Check In added) are removed so every
-- outlet / HUB account has the same menus; they are kept in user_page_backup_20261111.
CREATE TABLE IF NOT EXISTS integration_portal.user_page_backup_20261111 AS
SELECT up.* FROM integration_portal.user_page up JOIN integration_portal.user_account u ON u.id = up.user_id WHERE u.role = 'outlet';

DELETE FROM integration_portal.role_page WHERE role = 'outlet'
   AND page NOT IN ('stock-opname', 'waste', 'form.stock-transfer', 'form.damaged-equipment', 'form.pengajuan',
                    'rating.grabfood', 'rating.google-maps', 'rating.shopeefood', 'rating.gofood', 'guide');
INSERT INTO integration_portal.role_page (role, page)
SELECT 'outlet', p FROM unnest(ARRAY['stock-opname', 'waste', 'form.stock-transfer', 'form.damaged-equipment', 'form.pengajuan',
                                     'rating.grabfood', 'rating.google-maps', 'rating.shopeefood', 'rating.gofood', 'guide']) p
ON CONFLICT DO NOTHING;

DELETE FROM integration_portal.user_page up USING integration_portal.user_account u WHERE u.id = up.user_id AND u.role = 'outlet';
