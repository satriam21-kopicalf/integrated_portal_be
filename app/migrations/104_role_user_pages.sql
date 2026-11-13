-- 104: role User (Area Supervisor / PIC, Kopi Calf and Wasgee) opens exactly (2026-11-13): Stock Opname, Waste, Fund
-- Request, Damaged Equipment, PIC Check In, Product, Outlet, Transfer Stock, Dashboard, Sales Transactions, the
-- Aggregator (GrabFood, Google Maps, ShopeeFood, GoFood) and the Guide — never Cost Control Clock In. The exceptions per
-- account that gave other pages are removed, except the SWAT team markers (form.wwwr, mon.wwwr, set per account) and the
-- accounts of the Calf On The Road line (their COTR pages come from these exceptions). Kept in *_backup_20261113.
CREATE TABLE IF NOT EXISTS integration_portal.role_page_backup_20261113 AS
SELECT * FROM integration_portal.role_page WHERE role = 'user';
CREATE TABLE IF NOT EXISTS integration_portal.user_page_backup_20261113 AS
SELECT up.* FROM integration_portal.user_page up JOIN integration_portal.user_account u ON u.id = up.user_id WHERE u.role = 'user';

DELETE FROM integration_portal.role_page WHERE role = 'user'
   AND page NOT IN ('overview', 'sales', 'stock-opname', 'waste', 'form.pengajuan', 'form.damaged-equipment', 'form.pic-check-in',
                    'form.product', 'form.outlet', 'form.stock-transfer', 'rating.grabfood', 'rating.google-maps',
                    'rating.shopeefood', 'rating.gofood', 'guide');
INSERT INTO integration_portal.role_page (role, page)
SELECT 'user', p FROM unnest(ARRAY['overview', 'sales', 'stock-opname', 'waste', 'form.pengajuan', 'form.damaged-equipment',
                                   'form.pic-check-in', 'form.product', 'form.outlet', 'form.stock-transfer', 'rating.grabfood',
                                   'rating.google-maps', 'rating.shopeefood', 'rating.gofood', 'guide']) p
ON CONFLICT DO NOTHING;

DELETE FROM integration_portal.user_page up USING integration_portal.user_account u
 WHERE u.id = up.user_id AND u.role = 'user' AND coalesce(u.business_line, '') <> 'cotr'
   AND up.allowed AND up.page NOT IN ('form.wwwr', 'mon.wwwr')
   AND up.page NOT IN ('overview', 'sales', 'stock-opname', 'waste', 'form.pengajuan', 'form.damaged-equipment', 'form.pic-check-in',
                       'form.product', 'form.outlet', 'form.stock-transfer', 'rating.grabfood', 'rating.google-maps',
                       'rating.shopeefood', 'rating.gofood', 'guide');
