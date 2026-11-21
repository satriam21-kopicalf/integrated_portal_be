-- 129: the stock moves of the outlets per document line, with every Cost Control correction, for the ERP stock ledger
-- (erp-calfcoffeeindustry WP-2.6, D-61, 2026-11-21). One definition of the moves: the same as app/stock_moves.py
--   * ESB documents (integration_esb.erp_documents + lines) of the kinds of MOVES, drafts / rejected / cancelled / void
--     left out, the GRs entered on a received Transfer Stock left out (the transfer brings their goods in);
--   * a GR marked Finished by Cost Control: its corrected receipt date and line quantities (cc_gr_correction, 072/077);
--   * the other documents Cost Control corrected (cc_doc_fix, 097): the corrected quantity of the document x item x unit,
--     shared over its lines;
--   * the Transfer Stock between outlets (075): out of the source when Cost Control approved, in the requester when
--     received.
-- Read only, no photo / evidence / personal column (the correction note has the reason and time, not who: docs/13 §2 of the ERP). The ERP reads it through FDW (erp backend/sql/17-18) and posts one
-- voucher per document; a document whose content changes is reversed and posted again there.

CREATE OR REPLACE VIEW integration_portal.erp_stock_move_line AS
WITH kinds(module, line_type, kind, sign) AS (
  VALUES ('goods_receipt', 'detail', 'received', 1), ('simple_purchase', 'detail', 'outletBuy', 1),
         ('simple_manufacturing', 'result', 'produced', 1), ('simple_manufacturing', 'material', 'consumed', -1),
         ('goods_delivery', 'detail', 'delivered', -1), ('purchase_return', 'detail', 'returned', -1),
         ('goods_receipt_return', 'detail', 'returned', -1)
),
linked AS (
  SELECT upper(gr_number) AS doc_num FROM integration_portal.stock_transfer WHERE status = 'received' AND gr_number IS NOT NULL
),
esb AS (
  SELECT d.module, d.doc_num, l.line_type, l.line_no, d.doc_date, b.branch_code, k.kind, k.sign,
         l.product_code, l.product_name, upper(coalesce(l.uom_name, '')) AS uom, l.qty, l.price,
         d.counterparty_name, d.ref_num, d.status_name, fin.doc_num IS NOT NULL AS finished
    FROM integration_esb.erp_documents d
    JOIN kinds k ON k.module = d.module
    JOIN integration_esb.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = k.line_type
    JOIN integration_esb.master_branches b ON b.branch_id::text = d.branch_id::text
    LEFT JOIN integration_portal.cc_gr_finish fin ON d.module = 'goods_receipt' AND fin.doc_num = d.doc_num
   WHERE d.deleted_at IS NULL
     AND coalesce(d.status_name, '') NOT IN ('New', 'Draft', 'Rejected', 'Cancelled', 'Canceled', 'Void')
     AND l.product_code IS NOT NULL AND coalesce(l.qty, 0) <> 0
     AND NOT (d.module = 'goods_receipt' AND upper(d.doc_num) IN (SELECT doc_num FROM linked))
),
fix AS (  -- the latest active correction per document x line type x item x unit (097)
  SELECT DISTINCT ON (module, doc_num, line_type, product_code, uom) module, doc_num, line_type, product_code, uom,
         new_qty, reason, created_at
    FROM integration_portal.cc_doc_fix WHERE cancelled_at IS NULL
   ORDER BY module, doc_num, line_type, product_code, uom, created_at DESC
),
esb_fixed AS (
  SELECT e.*,
         sum(e.qty) OVER (PARTITION BY e.module, e.doc_num, e.line_type, e.product_code, e.uom) AS key_qty,
         dc.new_date, dc.reason AS date_reason, dc.created_at AS date_at,
         qc.new_qty AS gr_qty, qc.reason AS qty_reason, qc.created_at AS qty_at,
         f.new_qty AS fix_qty, f.reason AS fix_reason, f.created_at AS fix_at,
         po.price AS po_price
    FROM esb e
    LEFT JOIN integration_portal.cc_gr_correction dc ON e.finished AND dc.doc_num = e.doc_num AND dc.kind = 'date'
         AND dc.replaced_at IS NULL AND dc.cancelled_at IS NULL
    LEFT JOIN integration_portal.cc_gr_correction qc ON e.finished AND qc.doc_num = e.doc_num AND qc.kind = 'qty'
         AND qc.line_no = e.line_no AND qc.replaced_at IS NULL AND qc.cancelled_at IS NULL
    LEFT JOIN fix f ON f.module = e.module AND f.doc_num = e.doc_num AND f.line_type = e.line_type
         AND f.product_code = e.product_code AND f.uom = e.uom
    LEFT JOIN LATERAL (SELECT p.price FROM integration_esb.erp_document_lines p
                        WHERE e.module = 'goods_receipt' AND p.module = 'purchase_order' AND p.doc_num = e.ref_num
                          AND p.line_type = 'detail' AND p.product_code = e.product_code AND p.price > 0
                        ORDER BY (upper(coalesce(p.uom_name, '')) = e.uom) DESC LIMIT 1) po ON true
)
SELECT 'esb'::text AS source_kind, module, doc_num, line_type || ':' || coalesce(line_no::text, '0') AS line_key,
       branch_code, kind, doc_date, coalesce(new_date, doc_date) AS posting_date,
       product_code AS item_code, product_name AS item_name, uom,
       sign * CASE WHEN gr_qty IS NOT NULL THEN gr_qty
                   WHEN fix_qty IS NOT NULL AND key_qty <> 0 THEN fix_qty * qty / key_qty
                   ELSE qty END AS qty,
       sign * qty AS esb_qty,
       coalesce(nullif(price, 0), po_price) AS price, counterparty_name AS counterparty, ref_num, status_name AS status,
       (new_date IS NOT NULL OR gr_qty IS NOT NULL OR fix_qty IS NOT NULL) AS corrected,
       nullif(concat_ws(' · ',
         CASE WHEN new_date IS NOT NULL THEN format('Tanggal %s → %s: %s (%s)', doc_date, new_date, date_reason, to_char(date_at AT TIME ZONE 'Asia/Jakarta', 'YYYY-MM-DD HH24:MI')) END,
         CASE WHEN gr_qty IS NOT NULL THEN format('Qty %s → %s: %s (%s)', qty, gr_qty, qty_reason, to_char(qty_at AT TIME ZONE 'Asia/Jakarta', 'YYYY-MM-DD HH24:MI')) END,
         CASE WHEN fix_qty IS NOT NULL AND gr_qty IS NULL THEN format('Qty %s → %s: %s (%s)', key_qty, fix_qty, fix_reason, to_char(fix_at AT TIME ZONE 'Asia/Jakarta', 'YYYY-MM-DD HH24:MI')) END), '') AS correction_note
  FROM esb_fixed
UNION ALL
SELECT 'transfer', 'stock_transfer', t.number, x.kind || ':' || l.line_no, x.branch, x.kind,
       (x.at AT TIME ZONE 'Asia/Jakarta')::date, (x.at AT TIME ZONE 'Asia/Jakarta')::date,
       l.item_code, l.item_name, upper(coalesce(l.unit, '')), x.sign * l.qty, x.sign * l.qty, l.price,
       x.other, NULL, t.status, false, NULL
  FROM integration_portal.stock_transfer t
  JOIN integration_portal.stock_transfer_line l ON l.transfer_id = t.id
  CROSS JOIN LATERAL (VALUES (t.to_branch, 'transferIn', 1, t.received_at, t.status = 'received', t.from_name),
                             (t.from_branch, 'transferOut', -1, coalesce(t.cc_at, t.received_at), t.status IN ('approved', 'received'), t.to_name))
                     x(branch, kind, sign, at, counts, other)
 WHERE x.counts AND x.at IS NOT NULL;

COMMENT ON VIEW integration_portal.erp_stock_move_line IS
  'Stock moves of the outlets per document line with the Cost Control corrections, for the ERP stock ledger (D-61); same definition as app/stock_moves.py';
