-- 161: Fund Request and Maintenance / Procurement reset (2026-11-27, asked by the user after the testing of the whole
-- Fund Request flow): every request is test data and is removed, as 070. Every row is kept first in a *_backup_20261127
-- table; drop those tables once they are no longer needed. The masters and settings stay: units, flows, rules, options,
-- plafon, categories, technicians. The numbers start again from 1. On a fresh database this only creates empty backups.

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_backup_20261127 AS SELECT * FROM integration_portal.pengajuan;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_line_backup_20261127 AS SELECT * FROM integration_portal.pengajuan_line;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_file_backup_20261127 AS SELECT * FROM integration_portal.pengajuan_file;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_event_backup_20261127 AS SELECT * FROM integration_portal.pengajuan_event;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_counter_backup_20261127 AS SELECT * FROM integration_portal.pengajuan_counter;
CREATE TABLE IF NOT EXISTS integration_portal.de_request_backup_20261127 AS SELECT * FROM integration_portal.de_request;
CREATE TABLE IF NOT EXISTS integration_portal.de_item_backup_20261127 AS SELECT * FROM integration_portal.de_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_media_backup_20261127 AS SELECT * FROM integration_portal.de_media;
CREATE TABLE IF NOT EXISTS integration_portal.de_event_backup_20261127 AS SELECT * FROM integration_portal.de_event;
CREATE TABLE IF NOT EXISTS integration_portal.de_assignment_backup_20261127 AS SELECT * FROM integration_portal.de_assignment;
CREATE TABLE IF NOT EXISTS integration_portal.de_work_link_backup_20261127 AS SELECT * FROM integration_portal.de_work_link;
CREATE TABLE IF NOT EXISTS integration_portal.de_cost_item_backup_20261127 AS SELECT * FROM integration_portal.de_cost_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_counter_backup_20261127 AS SELECT * FROM integration_portal.de_counter;

-- the lines, files and events go with the request (ON DELETE CASCADE); the links between requests first
UPDATE integration_portal.pengajuan SET settles_id = NULL
    WHERE settles_id IN (SELECT id FROM integration_portal.pengajuan_backup_20261127);
DELETE FROM integration_portal.pengajuan p
    WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_backup_20261127 b WHERE b.id = p.id);
DELETE FROM integration_portal.pengajuan_counter c
    WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_counter_backup_20261127 b WHERE b.* = c.*);
-- the request's items, media, events, assignments, work links and cost lines go with it (ON DELETE CASCADE)
UPDATE integration_portal.de_request SET duplicate_of = NULL
    WHERE duplicate_of IN (SELECT id FROM integration_portal.de_request_backup_20261127);
DELETE FROM integration_portal.de_request r
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_request_backup_20261127 b WHERE b.id = r.id);
DELETE FROM integration_portal.de_counter c
    WHERE EXISTS (SELECT 1 FROM integration_portal.de_counter_backup_20261127 b WHERE b.month = c.month);
