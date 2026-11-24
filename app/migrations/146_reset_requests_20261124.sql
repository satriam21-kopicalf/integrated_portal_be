-- 146: reset of Maintenance, Procurement and Fund Request (2026-11-24, requested by the user with the new life of a Fund
-- Request): every request sent or drafted so far is removed and the numbers start again. Every row is kept first in a
-- *_backup_20261124 table; the attachment files stay on the backend volume (their paths are in the backups). The settings
-- stay: approval steps, rules, lists, plafon, categories and technicians. Only the rows in the backups are removed.
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_backup_20261124 AS SELECT * FROM integration_portal.pengajuan;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_line_backup_20261124 AS SELECT * FROM integration_portal.pengajuan_line;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_file_backup_20261124 AS SELECT * FROM integration_portal.pengajuan_file;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_event_backup_20261124 AS SELECT * FROM integration_portal.pengajuan_event;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_counter_backup_20261124 AS SELECT * FROM integration_portal.pengajuan_counter;
CREATE TABLE IF NOT EXISTS integration_portal.de_request_backup_20261124 AS SELECT * FROM integration_portal.de_request;
CREATE TABLE IF NOT EXISTS integration_portal.de_item_backup_20261124 AS SELECT * FROM integration_portal.de_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_media_backup_20261124 AS SELECT * FROM integration_portal.de_media;
CREATE TABLE IF NOT EXISTS integration_portal.de_event_backup_20261124 AS SELECT * FROM integration_portal.de_event;
CREATE TABLE IF NOT EXISTS integration_portal.de_assignment_backup_20261124 AS SELECT * FROM integration_portal.de_assignment;
CREATE TABLE IF NOT EXISTS integration_portal.de_work_link_backup_20261124 AS SELECT * FROM integration_portal.de_work_link;
CREATE TABLE IF NOT EXISTS integration_portal.de_cost_item_backup_20261124 AS SELECT * FROM integration_portal.de_cost_item;
CREATE TABLE IF NOT EXISTS integration_portal.de_counter_backup_20261124 AS SELECT * FROM integration_portal.de_counter;

UPDATE integration_portal.pengajuan SET settles_id = NULL WHERE settles_id IN (SELECT id FROM integration_portal.pengajuan_backup_20261124);
DELETE FROM integration_portal.pengajuan p WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_backup_20261124 b WHERE b.id = p.id);
UPDATE integration_portal.pengajuan SET de_request_id = NULL WHERE de_request_id IN (SELECT id FROM integration_portal.de_request_backup_20261124);
UPDATE integration_portal.de_request SET duplicate_of = NULL WHERE duplicate_of IN (SELECT id FROM integration_portal.de_request_backup_20261124);
DELETE FROM integration_portal.de_request r WHERE EXISTS (SELECT 1 FROM integration_portal.de_request_backup_20261124 b WHERE b.id = r.id);
DELETE FROM integration_portal.pengajuan_counter c WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_counter_backup_20261124 b WHERE b.month = c.month);
DELETE FROM integration_portal.de_counter c WHERE EXISTS (SELECT 1 FROM integration_portal.de_counter_backup_20261124 b WHERE b.month = c.month);
