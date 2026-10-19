-- 022: PIC Check In reset (2026-10-19, requested by the owner: the trial check-ins made before the outside-radius alert
-- and the fake GPS check went live are removed). Every row is kept first in pic_checkin_backup_20261019 (photos
-- included); drop that table once it is no longer needed. On a fresh database this only creates an empty backup.

CREATE TABLE IF NOT EXISTS integration_portal.pic_checkin_backup_20261019 AS
    SELECT * FROM integration_portal.pic_checkin;
DELETE FROM integration_portal.pic_checkin c
    WHERE EXISTS (SELECT 1 FROM integration_portal.pic_checkin_backup_20261019 b WHERE b.id = c.id);
