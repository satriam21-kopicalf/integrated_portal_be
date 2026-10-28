-- 033: Facility accounts open Stock Opname and Waste only (2026-10-28, user request). The accounts used by the outlets,
-- the Central Kitchen hubs and the Warehouse hubs (outlet.*, hubck.*, hubwh.*, the Wasgee Tea outlets wasgee.* and the
-- central facilities wh.*, ck.pusat, roastery.pusat) get per-user exceptions on top of role "user": every page of the
-- role is removed except "stock-opname", and "waste" is added. A superadmin can still change one account in User
-- Accounts → Page access. Their previous exceptions are kept in user_page_backup_20261028.
-- On a fresh database this only creates an empty backup.

CREATE TABLE IF NOT EXISTS integration_portal.user_page_backup_20261028 AS
    SELECT p.* FROM integration_portal.user_page p JOIN integration_portal.user_account u ON u.id = p.user_id
    WHERE u.role = 'user' AND (split_part(u.username, '.', 1) IN ('outlet', 'hubck', 'hubwh', 'wasgee', 'wh')
                               OR u.username IN ('ck.pusat', 'roastery.pusat'));

CREATE TEMP TABLE facility_account ON COMMIT DROP AS
    SELECT id FROM integration_portal.user_account
    WHERE role = 'user' AND (split_part(username, '.', 1) IN ('outlet', 'hubck', 'hubwh', 'wasgee', 'wh')
                             OR username IN ('ck.pusat', 'roastery.pusat'));

DELETE FROM integration_portal.user_page WHERE user_id IN (SELECT id FROM facility_account);
-- the role's pages other than Stock Opname / Waste: removed
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT a.id, r.page, false FROM facility_account a
    JOIN integration_portal.role_page r ON r.role = 'user' AND r.page NOT IN ('stock-opname', 'waste');
-- Stock Opname / Waste the role does not open: added
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT a.id, p.page, true FROM facility_account a CROSS JOIN (VALUES ('stock-opname'), ('waste')) p(page)
    WHERE NOT EXISTS (SELECT 1 FROM integration_portal.role_page r WHERE r.role = 'user' AND r.page = p.page);
