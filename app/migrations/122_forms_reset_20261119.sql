-- 122: Forms reset (2026-11-19, requested by the user once the PWA forms and the attendance link were done): every PIC
-- Check In (kind pic; the Cost Control / surveyor check-ins stay), every Product and Outlet visit and every WW-WR schedule
-- (its audit, members, action plans and visits) is removed. Every row is kept first in a *_backup_20261119 table (photos
-- and signatures included). The masters and settings stay. The attendance punch-ins from this day on come back as
-- PIC Check In (app/pic_checkin.ATTENDANCE_FROM). On a fresh database this only creates empty backups.

CREATE TABLE IF NOT EXISTS integration_portal.pic_checkin_backup_20261119 AS
SELECT * FROM integration_portal.pic_checkin WHERE kind = 'pic';
CREATE TABLE IF NOT EXISTS integration_portal.visit_form_backup_20261119 AS
SELECT * FROM integration_portal.visit_form WHERE form IN ('product', 'outlet', 'wwwr');
CREATE TABLE IF NOT EXISTS integration_portal.visit_form_photo_backup_20261119 AS
SELECT p.* FROM integration_portal.visit_form_photo p
 WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_backup_20261119 b WHERE b.id = p.visit_id);
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_backup_20261119 AS SELECT * FROM integration_portal.wwwr_audit;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_member_backup_20261119 AS SELECT * FROM integration_portal.wwwr_audit_member;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_action_backup_20261119 AS SELECT * FROM integration_portal.wwwr_action;

-- only the rows that are in the backups (a row added between the statements stays)
-- WW-WR: the members, action plans and evaluations go with the audit (ON DELETE CASCADE); the audit first
DELETE FROM integration_portal.wwwr_audit a
    WHERE EXISTS (SELECT 1 FROM integration_portal.wwwr_audit_backup_20261119 b WHERE b.id = a.id);
DELETE FROM integration_portal.visit_form_photo p
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_photo_backup_20261119 b WHERE b.id = p.id);
DELETE FROM integration_portal.visit_form v
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_backup_20261119 b WHERE b.id = v.id);
DELETE FROM integration_portal.pic_checkin c
    WHERE EXISTS (SELECT 1 FROM integration_portal.pic_checkin_backup_20261119 b WHERE b.id = c.id);
