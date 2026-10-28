-- 035: Area Supervisors move to Operations (2026-10-28, user request). The Area Supervisors who had Cost Control
-- Monitor / Review open the forms (PIC Check In, Monitoring Product, Monitoring Outlet, Damaged Equipment), Sales
-- Transactions and the Dashboard only: every page Operations has, so they sign in to operations.kopicalf.co.id and the
-- portal refuses them (app/access.py operations_only). Their previous exceptions are kept in
-- user_page_backup_20261028b. On a fresh database this only creates an empty backup.

CREATE TEMP TABLE area_supervisor ON COMMIT DROP AS
    SELECT u.id FROM integration_portal.user_account u
    WHERE u.role = 'user' AND u.job_title = 'Area Supervisor'
      AND EXISTS (SELECT 1 FROM integration_portal.user_page p
                  WHERE p.user_id = u.id AND p.page IN ('cc.monitor', 'cc.reviews') AND p.allowed);

CREATE TABLE IF NOT EXISTS integration_portal.user_page_backup_20261028b AS
    SELECT p.* FROM integration_portal.user_page p WHERE p.user_id IN (SELECT id FROM area_supervisor);

CREATE TEMP TABLE supervisor_page ON COMMIT DROP AS
    SELECT unnest(ARRAY['overview', 'sales', 'form.pic-check-in', 'form.product', 'form.outlet', 'form.damaged-equipment']) AS page;

DELETE FROM integration_portal.user_page WHERE user_id IN (SELECT id FROM area_supervisor);
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT a.id, r.page, false FROM area_supervisor a
    JOIN integration_portal.role_page r ON r.role = 'user' AND r.page NOT IN (SELECT page FROM supervisor_page);
INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT a.id, s.page, true FROM area_supervisor a CROSS JOIN supervisor_page s
    WHERE NOT EXISTS (SELECT 1 FROM integration_portal.role_page r WHERE r.role = 'user' AND r.page = s.page);
