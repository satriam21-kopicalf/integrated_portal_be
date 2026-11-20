-- 125: Fund Request reset (2026-11-20, requested by the user with the personal requests of migration 124): every Fund
-- Request sent or drafted so far (outlet and personal: its expense lines, attachments and history) is removed and the
-- numbers start again (PGJ-YYMM-0001). Every row is kept first in a *_backup_20261120 table; the attachment files stay
-- on the backend volume (their paths are in the backup). Drop those tables once they are no longer needed. The settings
-- stay: approval steps, petty cash plafon, the personal lists (fund_option) and the department plafon.

CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_backup_20261120 AS SELECT * FROM integration_portal.pengajuan;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_line_backup_20261120 AS SELECT * FROM integration_portal.pengajuan_line;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_file_backup_20261120 AS SELECT * FROM integration_portal.pengajuan_file;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_event_backup_20261120 AS SELECT * FROM integration_portal.pengajuan_event;
CREATE TABLE IF NOT EXISTS integration_portal.pengajuan_counter_backup_20261120 AS SELECT * FROM integration_portal.pengajuan_counter;

-- only the rows in the backup (a request made between the statements stays); lines, files and events go with it
UPDATE integration_portal.pengajuan SET settles_id = NULL
    WHERE settles_id IN (SELECT id FROM integration_portal.pengajuan_backup_20261120);
DELETE FROM integration_portal.pengajuan p
    WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_backup_20261120 b WHERE b.id = p.id);
DELETE FROM integration_portal.pengajuan_counter c
    WHERE EXISTS (SELECT 1 FROM integration_portal.pengajuan_counter_backup_20261120 b WHERE b.month = c.month);
