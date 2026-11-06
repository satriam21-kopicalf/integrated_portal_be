-- 076: Transfer Stock — Cost Control adjusts the quantities when approving (2026-11-07). qty becomes the approved
-- quantity (what moves when received and what the COGS counts); requested_qty keeps what the outlet asked.
ALTER TABLE integration_portal.stock_transfer_line ADD COLUMN IF NOT EXISTS requested_qty numeric(20,6);
UPDATE integration_portal.stock_transfer_line SET requested_qty = qty WHERE requested_qty IS NULL;
