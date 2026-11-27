-- 160: Fund Request (2026-11-27, Finance): a Deposit is closed only when the requester has attached the proof after the
-- transfer and Finance has checked it (In Progress → Verifikasi → Done), like Deposit / Cash in Advance. The Keperluan Dana
-- "Deposit" (department plafon) was done at the transfer.
INSERT INTO integration_portal.fund_rule (key, proof_after, proof_days, proof_docs, vendor)
SELECT 'PERSONAL:' || o.id, true, 7, ARRAY['NOTA'], false
  FROM integration_portal.fund_option o
 WHERE o.list = 'PURPOSE' AND o.name = 'Deposit'
ON CONFLICT (key) DO UPDATE SET proof_after = true, proof_docs = CASE WHEN cardinality(integration_portal.fund_rule.proof_docs) = 0
                                                                     THEN ARRAY['NOTA'] ELSE integration_portal.fund_rule.proof_docs END;
