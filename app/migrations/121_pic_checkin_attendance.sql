-- 121: PIC Check In from the attendance (2026-11-19, the user's decision): a PIC punches in at the first outlet of the
-- day (HRMS attendance, face and GPS checked there); that punch becomes the PIC Check In of the outlet whose radius it
-- falls in, so the PIC does not check in twice. The other outlets of the day still go through Form → PIC Check In.
-- `source` tells the two apart; the attendance row keeps its HRMS log id (one PIC Check In per punch) and has no photo
-- of its own (the face is in the HRMS attendance).

ALTER TABLE integration_portal.pic_checkin ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'form';
ALTER TABLE integration_portal.pic_checkin ADD COLUMN IF NOT EXISTS attendance_id integer;
ALTER TABLE integration_portal.pic_checkin ALTER COLUMN photo DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pic_checkin_attendance_uniq ON integration_portal.pic_checkin (attendance_id) WHERE attendance_id IS NOT NULL;
