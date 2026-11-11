-- 090: the branch of the ESB company COTR (CALF5, schema integration_esb_cotr) each pool / HUB is (2026-11-11): the
-- simple sales, simple transfers and cupping / waste journals of that branch feed the product stock of the pool.
ALTER TABLE integration_portal.cotr_pool ADD COLUMN IF NOT EXISTS esb_branch_code text;
UPDATE integration_portal.cotr_pool p SET esb_branch_code = v.code
  FROM (VALUES ('HUB-CPT', 'COTR5'), ('HUB-KWT', 'KTG'), ('HUB-PLM', 'PAL'), ('HUB-PSMG', 'PASMI'), ('HUB-TBT', 'TBT'), ('STK-PLM', 'HOF')) v (pool, code)
 WHERE p.code = v.pool AND p.esb_branch_code IS NULL;
