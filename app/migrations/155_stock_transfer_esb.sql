-- 155: Transfer Stock, the flow of Cost Control (2026-11-26). The destination outlet asks; it is pending until Cost Control
-- approves (quantities may be adjusted) or rejects; Cost Control then makes it in ESB and marks it processed with the ESB
-- document number. The source outlet only sees it. For now a transfer moves no stock in the portal (app/stock_transfer.py
-- COUNTS_STOCK): ESB holds the movement.
ALTER TABLE integration_portal.stock_transfer DROP CONSTRAINT IF EXISTS stock_transfer_status_check;
ALTER TABLE integration_portal.stock_transfer ADD CONSTRAINT stock_transfer_status_check
    CHECK (status IN ('submitted', 'accepted', 'declined', 'approved', 'rejected', 'processed', 'received', 'cancelled'));
ALTER TABLE integration_portal.stock_transfer ADD COLUMN IF NOT EXISTS esb_number text;
ALTER TABLE integration_portal.stock_transfer ADD COLUMN IF NOT EXISTS esb_note   text;
ALTER TABLE integration_portal.stock_transfer ADD COLUMN IF NOT EXISTS esb_by     text;
ALTER TABLE integration_portal.stock_transfer ADD COLUMN IF NOT EXISTS esb_at     timestamptz;

-- the ones the source outlet already accepted wait for Cost Control as the new ones do
UPDATE integration_portal.stock_transfer SET status = 'submitted' WHERE status = 'accepted';
