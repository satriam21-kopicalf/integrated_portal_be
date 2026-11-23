-- 2026-11-23 (the user: "Arrived / Transfer belum ada tujuan pool"): a transfer between two pools is entered once and
-- kept as two rows, out at the pool it leaves (qty < 0) and in at the pool it reaches (qty > 0), tied by `ref`; `peer_pool`
-- is the other pool of the pair. Arrived rows keep the pool they reached; their peer is the pool / storage it came from
-- when given. Older rows (one-sided, minus = out) stay as they are.
ALTER TABLE integration_portal.cotr_pool_move ADD COLUMN IF NOT EXISTS peer_pool text;
ALTER TABLE integration_portal.cotr_pool_move ADD COLUMN IF NOT EXISTS ref uuid;
CREATE INDEX IF NOT EXISTS cotr_pool_move_ref ON integration_portal.cotr_pool_move (ref) WHERE ref IS NOT NULL;
