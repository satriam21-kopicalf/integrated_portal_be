-- 131: a PIC's attendance punch-in is the check in of an OUTLET only (2026-11-21). A hub (HUB_WH / HUB_CK) sits inside
-- an outlet, inside its radius, and took some punches; those check-ins go and the monitoring brings the punch back at
-- the outlet (app/pic_checkin.py from_attendance / sync_attendance). Check-ins sent through the form stay as they are.
DELETE FROM integration_portal.pic_checkin c
 USING kc_master.facility f
 WHERE f.code = c.branch_code AND c.source = 'attendance' AND f.facility_type <> 'OUTLET';
