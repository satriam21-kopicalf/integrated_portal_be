-- 142: the cost lines of Maintenance & Procurement follow the Fund Request categories (2026-11-24, user decision): every
-- line takes a Jenis Transaksi (fund_option list TRANSACTION, set by management in Monitoring Fund Request → Pengaturan),
-- so the fund asked from a request carries the same categories Finance works with. The old fixed kinds (Material / Jasa /
-- Transport / Lainnya) are mapped once to the closest Jenis Transaksi; `kind` stays for the history.
ALTER TABLE integration_portal.de_cost_item ADD COLUMN IF NOT EXISTS transaction_id integer REFERENCES integration_portal.fund_option (id);

UPDATE integration_portal.de_cost_item c SET transaction_id = o.id
  FROM integration_portal.de_request r, integration_portal.fund_option o
 WHERE c.request_id = r.id AND c.transaction_id IS NULL AND o.list = 'TRANSACTION'
   AND o.name = CASE
         WHEN c.kind = 'TRANSPORT' THEN 'Accommodation (Fuel, E-toll)/Tickets/Shipping Costs'
         WHEN r.kind = 'PROCUREMENT' AND c.kind = 'MATERIAL' THEN 'Purchase of Tools/Assets/Machinery'
         WHEN c.kind IN ('MATERIAL', 'SERVICE') THEN 'Materials/Labor Costs/Workshop (Outlet Maintenance/Renovation)'
         ELSE 'General (Kitchen Supplies/Cleaning Supplies, etc.)' END;
