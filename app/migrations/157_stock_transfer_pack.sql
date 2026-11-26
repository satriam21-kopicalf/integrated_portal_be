-- 157: Transfer Stock in whole units (2026-11-26, Cost Control): the materials and their unit come from the stock opname
-- template of HUB WH / HUB CK (kc_inventory count_list_item.pack_uom, e.g. CTN@24PCS); the outlet asks in that unit and the
-- base quantity (qty, GR / PCS / ML) is pack_qty × pack_factor. Every transfer entered before is removed (asked by Cost
-- Control): the numbering starts again.
ALTER TABLE integration_portal.stock_transfer_line ADD COLUMN IF NOT EXISTS pack_uom    text;
ALTER TABLE integration_portal.stock_transfer_line ADD COLUMN IF NOT EXISTS pack_factor numeric(20,6);
ALTER TABLE integration_portal.stock_transfer_line ADD COLUMN IF NOT EXISTS pack_qty    numeric(20,6);
ALTER TABLE integration_portal.stock_transfer_line ADD COLUMN IF NOT EXISTS requested_pack_qty numeric(20,6);

DELETE FROM integration_portal.stock_transfer;  -- the lines go with them (ON DELETE CASCADE)
