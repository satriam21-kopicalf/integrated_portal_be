-- 013: Outlet ratio (Cost Control, app/ratio.py): manual corrections the Cost Control team makes in its workbook
-- "CALF - Rasio Outlet": the stock value of an outlet's stock opname on a date (SO awal / SO akhir) and the MDR
-- (platform & payment fees) of a month that turns gross sales into "Pendapatan".

CREATE TABLE IF NOT EXISTS integration_portal.ratio_so_override (
    branch_code text        NOT NULL,
    so_date     date        NOT NULL,
    value       numeric(18, 2) NOT NULL CHECK (value >= 0),
    note        text        NOT NULL CHECK (btrim(note) <> ''),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (branch_code, so_date)
);
COMMENT ON TABLE integration_portal.ratio_so_override IS
    'stock opname value used by the outlet ratio instead of the ESB stock opname document of that date (with the reason)';

CREATE TABLE IF NOT EXISTS integration_portal.ratio_mdr (
    branch_code text        NOT NULL,
    month       date        NOT NULL CHECK (extract(day FROM month) = 1),
    mdr_pct     numeric(7, 4) NOT NULL CHECK (mdr_pct >= 0 AND mdr_pct < 100),
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid,
    PRIMARY KEY (branch_code, month)
);
COMMENT ON TABLE integration_portal.ratio_mdr IS 'MDR % of an outlet in a month: Pendapatan = gross sales x (1 - MDR)';
