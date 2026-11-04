-- 068: Forms reset (2026-11-04, requested by the user for a new round of testing): every PIC Check In (kind pic; the
-- Cost Control check-ins stay), Monitoring Product, Monitoring Outlet, WW-WR and Damaged Equipment record made so far is
-- removed, as 067. Every row is kept first in a *_backup_20261104 table (photos and signatures included); drop those
-- tables once they are no longer needed. The masters stay: branch groups, outlet locations, Damaged Equipment
-- categories and technicians, accounts and their pages. On a fresh database this only creates empty backups.

CREATE TABLE IF NOT EXISTS integration_portal.pic_checkin_backup_20261104 AS SELECT * FROM integration_portal.pic_checkin WHERE kind = 'pic';
CREATE TABLE IF NOT EXISTS integration_portal.visit_form_backup_20261104 AS SELECT * FROM integration_portal.visit_form;
CREATE TABLE IF NOT EXISTS integration_portal.visit_form_photo_backup_20261104 AS SELECT * FROM integration_portal.visit_form_photo;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_backup_20261104 AS SELECT * FROM integration_portal.wwwr_audit;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_audit_member_backup_20261104 AS SELECT * FROM integration_portal.wwwr_audit_member;
CREATE TABLE IF NOT EXISTS integration_portal.wwwr_action_backup_20261104 AS SELECT * FROM integration_portal.wwwr_action;
CREATE TABLE IF NOT EXISTS integration_portal.de_request_backup_20261104 AS SELECT * FROM integration_portal.de_request;
CREATE TABLE IF NOT EXISTS integration_portal.de_item_backup_20261104 AS SELECT * FROM integration_portal.de_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_media_backup_20261104 AS SELECT * FROM integration_portal.de_media;
CREATE TABLE IF NOT EXISTS integration_portal.de_event_backup_20261104 AS SELECT * FROM integration_portal.de_event;
CREATE TABLE IF NOT EXISTS integration_portal.de_assignment_backup_20261104 AS SELECT * FROM integration_portal.de_assignment;
CREATE TABLE IF NOT EXISTS integration_portal.de_work_link_backup_20261104 AS SELECT * FROM integration_portal.de_work_link;
CREATE TABLE IF NOT EXISTS integration_portal.de_cost_item_backup_20261104 AS SELECT * FROM integration_portal.de_cost_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_counter_backup_20261104 AS SELECT * FROM integration_portal.de_counter;

-- only the rows that are in the backups (a row added between the two statements stays)
-- WW-WR: the members and action plans go with the audit (ON DELETE CASCADE); the audit first, it points to its visit
DELETE FROM integration_portal.wwwr_audit a
    WHERE EXISTS (SELECT 1 FROM integration_portal.wwwr_audit_backup_20261104 b WHERE b.id = a.id);
DELETE FROM integration_portal.visit_form_photo p
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_photo_backup_20261104 b WHERE b.id = p.id);
DELETE FROM integration_portal.visit_form v
    WHERE EXISTS (SELECT 1 FROM integration_portal.visit_form_backup_20261104 b WHERE b.id = v.id);
DELETE FROM integration_portal.pic_checkin c
    WHERE EXISTS (SELECT 1 FROM integration_portal.pic_checkin_backup_20261104 b WHERE b.id = c.id);
-- the request's items, media, events, assignments, work links and cost lines go with it (ON DELETE CASCADE)
UPDATE integration_portal.de_request SET duplicate_of = NULL
    WHERE duplicate_of IN (SELECT id FROM integration_portal.de_request_backup_20261104);
DELETE FROM integration_portal.de_request r
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_request_backup_20261104 b WHERE b.id = r.id);
-- request numbers start again from 1 (DE-YYMM-0001)
DELETE FROM integration_portal.de_counter c
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_counter_backup_20261104 b WHERE b.month = c.month);
