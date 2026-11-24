-- 143: the money a technician needs for a Maintenance / Procurement job (2026-11-24, user decision): a Cash in Advance per
-- request, asked by the team for the technician assigned to it (technicians have no portal account). It is a personal
-- Fund Request (Keperluan Dana of type ADVANCE, its own approval flow) linked to the request (de_request_id), paid to the
-- technician's account, settled afterwards with the receipts (Laporan Penyelesaian). The technician's account is kept
-- for the next time.

-- "one fund per request" counts the request's fund only (MAINTENANCE / PROCUREMENT), not the technicians' advances
DROP INDEX IF EXISTS integration_portal.pengajuan_de_fund_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS pengajuan_de_fund_uniq ON integration_portal.pengajuan (de_request_id)
    WHERE de_request_id IS NOT NULL AND settles_id IS NULL AND status <> 'REJECTED' AND kind IN ('MAINTENANCE', 'PROCUREMENT');

ALTER TABLE integration_portal.de_technician
    ADD COLUMN IF NOT EXISTS bank           text,
    ADD COLUMN IF NOT EXISTS account_no     text,
    ADD COLUMN IF NOT EXISTS account_name   text;
