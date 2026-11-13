-- 107: Forms reset (2026-11-13, requested by the user after the Form Items editor): every Product and Outlet visit the
-- Area Supervisors made and every WW-WR schedule (its audit, members, action plans and its visits) is removed. Every row
-- is kept first in a *_backup_20261113 table (photos and signatures included); drop those tables once they are no longer
-- needed. PIC Check In, Damaged Equipment, the masters and the settings stay (branch groups, form items per group and
-- outlet, the Form Items editor of migration 106, outlet locations, accounts). On a fresh database this only creates
-- empty backups.

CREATE TABLE IF NOT EXISTS integration_portal.visit_form_backup_20261113 AS
SELECT * FROM integration_portal.visit_form WHERE form IN ('product', 'outlet', 'wwwr');
CREATE TABLE IF NOT EXISTS integration_portal.visit_form_photo_backup_20261113 AS
SELECT p.* FROM integration_portal.visit_form_photo p
 WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_backup_20261113 b WHERE b.id = p.visit_id);
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_backup_20261113 AS SELECT * FROM integration_portal.wwwr_audit;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_member_backup_20261113 AS SELECT * FROM integration_portal.wwwr_audit_member;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_action_backup_20261113 AS SELECT * FROM integration_portal.wwwr_action;

-- only the rows that are in the backups (a row added between the statements stays)
-- WW-WR: the members and action plans go with the audit (ON DELETE CASCADE); the audit first, it points to its visit
DELETE FROM integration_portal.wwwr_audit a
    WHERE EXISTS (SELECT 1 FROM integration_portal.wwwr_audit_backup_20261113 b WHERE b.id = a.id);
DELETE FROM integration_portal.visit_form_photo p
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_photo_backup_20261113 b WHERE b.id = p.id);
DELETE FROM integration_portal.visit_form v
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_backup_20261113 b WHERE b.id = v.id);
