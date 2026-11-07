-- 079: the ESB goods receipt of a Transfer Stock (2026-11-07). The requester enters the GR number when receiving the
-- goods; the portal matches it with the GR synced from ESB (app/stock_transfer.py gr_check) and that GR is no longer
-- counted as a purchase in the stock moves, since the transfer already brings the goods in (app/stock_moves.py).
ALTER TABLE integration_portal.stock_transfer ADD COLUMN IF NOT EXISTS gr_number text;
CREATE INDEX IF NOT EXISTS stock_transfer_gr_number ON integration_portal.stock_transfer (gr_number) WHERE gr_number IS NOT NULL;
