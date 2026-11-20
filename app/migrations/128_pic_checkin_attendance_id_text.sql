-- 128: the HRMS attendance ids are text (sk_hrms.attendance_records.id, e.g. 45b1ef32eb134771b74a), not integers
-- (2026-11-20). No row has an attendance_id yet: the punch-ins never came through (app/pic_checkin.py read a table that
-- does not exist).
ALTER TABLE integration_portal.pic_checkin ALTER COLUMN attendance_id TYPE text USING attendance_id::text;
