-- 072: Good Receipt corrections (2026-11-05). The GRs of ESB (integration_esb.erp_documents module goods_receipt, synced
-- by integrated-esbapi) stay as ESB has them; Cost Control corrects the receipt date of a GR or the quantity of a line,
-- with a reason and evidence, in a layer of its own. The COGS & Usage of the Sales Calculator and the Outlet Ratio use
-- the corrected figures (app/goods_receipt.py adjustments).
--   cc_gr_correction  kind date: the receipt date of the GR (line_no null); kind qty: the quantity of one line (its unit).
--                     One active correction per GR date / line (a new one replaces it, the old stays as history).
--   cc_gr_evidence    the files of a correction (photo of the delivery note, PDF …), at least one per correction.

CREATE TABLE IF NOT EXISTS integration_portal.cc_gr_correction (
    id            bigserial     PRIMARY KEY,
    doc_num       text          NOT NULL,
    kind          text          NOT NULL CHECK (kind IN ('date', 'qty')),
    line_no       integer,
    product_code  text,
    uom           text,
    old_date      date,
    new_date      date,
    old_qty       numeric(20,6),
    new_qty       numeric(20,6),
    reason        text          NOT NULL,
    created_by    text          NOT NULL,
    created_at    timestamptz   NOT NULL DEFAULT now(),
    replaced_at   timestamptz,
    cancelled_at  timestamptz,
    cancelled_by  text,
    CHECK ((kind = 'date' AND line_no IS NULL AND new_date IS NOT NULL)
        OR (kind = 'qty' AND line_no IS NOT NULL AND new_qty IS NOT NULL AND new_qty >= 0))
);
CREATE INDEX IF NOT EXISTS cc_gr_correction_doc ON integration_portal.cc_gr_correction (doc_num);
CREATE UNIQUE INDEX IF NOT EXISTS cc_gr_correction_active
    ON integration_portal.cc_gr_correction (doc_num, kind, coalesce(line_no, 0))
    WHERE replaced_at IS NULL AND cancelled_at IS NULL;

CREATE TABLE IF NOT EXISTS integration_portal.cc_gr_evidence (
    id             bigserial   PRIMARY KEY,
    correction_id  bigint      NOT NULL REFERENCES integration_portal.cc_gr_correction (id) ON DELETE CASCADE,
    doc_num        text        NOT NULL,
    file_name      text        NOT NULL,
    content_type   text        NOT NULL,
    bytes          integer     NOT NULL,
    data           bytea       NOT NULL,
    uploaded_by    text        NOT NULL,
    uploaded_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_gr_evidence_doc ON integration_portal.cc_gr_evidence (doc_num);

-- the page for the roles that open the Outlet Ratio or COGS & Usage
INSERT INTO integration_portal.role_page (role, page)
SELECT r.role, 'cc.goods-receipt' FROM integration_portal.role_page r WHERE r.page IN ('cc.ratio', 'cc.cogs')
ON CONFLICT DO NOTHING;
