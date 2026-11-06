-- 077: Good Receipt — the corrections of a GR count once Cost Control marks it Finished (2026-11-07). A correction made
-- or cancelled after that puts the GR back to draft until it is finished again; the stock (Stock List, actual usage)
-- and the COGS take only the finished corrections (app/goods_receipt.py adjustments).
CREATE TABLE IF NOT EXISTS integration_portal.cc_gr_finish (
    doc_num      text        PRIMARY KEY,
    note         text,
    finished_by  text        NOT NULL,
    finished_at  timestamptz NOT NULL DEFAULT now()
);
