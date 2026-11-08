-- 081: Calf On The Road storage & production stock (2026-11-08), as the Cost Control of Kopi Calf:
--   * cotr_receipt: the raw materials, packaging and supplies coming into a pool / hub (purchase, delivery);
--   * cotr_stock_count (+ lines): the stock opname of a pool / hub — draft, submitted, approved; the approved count
--     is where the running stock starts again;
--   * production waste: cups of a batch wasted while making it (cotr_batch.waste_qty) and raw materials wasted in the
--     production (cotr_waste.kind = 'production', with the batch).
-- Running stock of an item = the latest approved count + received since − used by the batches since (cups made × the
-- recipe card) − waste since.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_receipt (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    day        date NOT NULL,
    item_code  text NOT NULL REFERENCES integration_portal.cotr_item (code) ON UPDATE CASCADE,
    qty        numeric(14,3) NOT NULL CHECK (qty > 0),     -- in the base unit of the item
    supplier   text,
    doc_no     text,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid
);
CREATE INDEX IF NOT EXISTS cotr_receipt_pool_day ON integration_portal.cotr_receipt (pool_code, day);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_stock_count (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pool_code    text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    day          date NOT NULL,
    status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'approved')),
    note         text,
    created_by   uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    approved_by  uuid,
    approved_at  timestamptz
);
CREATE INDEX IF NOT EXISTS cotr_stock_count_pool ON integration_portal.cotr_stock_count (pool_code, day DESC);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_stock_count_line (
    count_id  uuid NOT NULL REFERENCES integration_portal.cotr_stock_count (id) ON DELETE CASCADE,
    item_code text NOT NULL REFERENCES integration_portal.cotr_item (code) ON UPDATE CASCADE,
    qty       numeric(14,3) NOT NULL CHECK (qty >= 0),      -- base unit
    expected  numeric(14,3),                                -- the running stock when it was counted
    note      text,
    PRIMARY KEY (count_id, item_code)
);

ALTER TABLE integration_portal.cotr_batch ADD COLUMN IF NOT EXISTS waste_qty integer NOT NULL DEFAULT 0 CHECK (waste_qty >= 0);
ALTER TABLE integration_portal.cotr_batch ADD COLUMN IF NOT EXISTS waste_reason text;
ALTER TABLE integration_portal.cotr_waste ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'storage' CHECK (kind IN ('storage', 'production'));
ALTER TABLE integration_portal.cotr_waste ADD COLUMN IF NOT EXISTS batch_id uuid REFERENCES integration_portal.cotr_batch (id) ON DELETE SET NULL;
ALTER TABLE integration_portal.cotr_waste ADD COLUMN IF NOT EXISTS item_code text;
