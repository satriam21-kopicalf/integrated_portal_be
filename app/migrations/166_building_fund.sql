-- 166: Building & Renovation → Fund Request (2026-11-27): the money of a project's RAB is asked from the RAB itself, its
-- lines chosen (a section, a store, or all): a pengajuan of kind BUILDING linked to the project (building_project_id),
-- every RAB line pointing to the fund that asked it (free again when that fund is rejected / cancelled). The proof after
-- the transfer: the invoice and a photo of the goods / work (rule BUILDING, Finance may change it).
ALTER TABLE integration_portal.pengajuan DROP CONSTRAINT IF EXISTS pengajuan_kind_check;
ALTER TABLE integration_portal.pengajuan ADD CONSTRAINT pengajuan_kind_check CHECK (kind IN ('PETTY_CASH', 'EMERGENCY', 'MARKETING', 'EVENT',
    'BILL', 'PERSONAL', 'MAINTENANCE', 'PROCUREMENT', 'VENDOR', 'INTERCOMPANY', 'BUILDING'));
ALTER TABLE integration_portal.pengajuan ADD COLUMN IF NOT EXISTS building_project_id uuid
    REFERENCES integration_portal.building_project(id) ON DELETE SET NULL;
ALTER TABLE integration_portal.building_rab_line ADD COLUMN IF NOT EXISTS fund_id uuid
    REFERENCES integration_portal.pengajuan(id) ON DELETE SET NULL;
INSERT INTO integration_portal.fund_rule (key, proof_after, proof_days, proof_docs, vendor)
VALUES ('BUILDING', true, 7, ARRAY['INVOICE', 'FOTO'], false)
ON CONFLICT (key) DO NOTHING;
