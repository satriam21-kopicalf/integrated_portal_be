-- 097: ESB documents with an unusual quantity, corrected by Cost Control (page cc.cogs, 2026-11-12; app/doc_checks.py).
-- A line of a document that moves the stock of an outlet (production, the outlet's own purchases, deliveries, returns)
-- whose quantity is far from the usual one (TGP04 entered its espresso production in KG instead of GR) gets the quantity
-- Cost Control gives, with the reason. ESB is never changed; the COGS & Usage, the Sales Calculator and the Stock List
-- read the corrected quantity (app/stock_moves.py). One active correction per line (product × unit of the document);
-- a new one replaces it, a cancelled one is kept as history. Goods receipts keep their own corrections (migration 072).
CREATE TABLE IF NOT EXISTS integration_portal.cc_doc_fix (
    id bigserial PRIMARY KEY,
    module text NOT NULL,
    doc_num text NOT NULL,
    line_type text NOT NULL,
    product_code text NOT NULL,
    uom text NOT NULL,
    branch_code text NOT NULL,
    doc_date date NOT NULL,
    old_qty numeric NOT NULL,
    new_qty numeric NOT NULL CHECK (new_qty >= 0),
    reason text NOT NULL,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    cancelled_by text
);
CREATE UNIQUE INDEX IF NOT EXISTS cc_doc_fix_active
    ON integration_portal.cc_doc_fix (module, doc_num, line_type, product_code, uom) WHERE cancelled_at IS NULL;
CREATE INDEX IF NOT EXISTS cc_doc_fix_date ON integration_portal.cc_doc_fix (doc_date);
