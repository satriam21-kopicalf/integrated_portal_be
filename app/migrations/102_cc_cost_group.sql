-- 102: cost group of an ESB item category (Cost Control · Outlet Ratio, purchases basis "GR", 2026-11-13; app/ratio.py).
-- The workbook groups the purchases by supplier (Calf Warehouse = BB, Central Kitchen = Espresso, CK Food = Dimsum);
-- management wants BB, Espresso, Dimsum and General Supplies apart, which a supplier does not tell (Calf Warehouse also
-- sends packaging and general supplies). Cost Control sets the group of every ESB category; a category without a row
-- follows its supplier as the workbook.
CREATE TABLE IF NOT EXISTS integration_portal.cc_cost_group (
    category text PRIMARY KEY,
    cost_group text NOT NULL CHECK (cost_group IN ('bb', 'espresso', 'dimsum', 'gs', 'other')),
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);
