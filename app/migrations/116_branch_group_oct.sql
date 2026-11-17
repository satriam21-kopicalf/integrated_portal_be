-- 116: Branch Group OCT (2026-11-17, user request). CCI14 Kopi Calf To Go BTN Senayan City counts its stock opname
-- with the OCT template (kc_inventory TPL-OCT); the portal had it in CTG. The two are kept in step: a new group OCT
-- with the settings of CTG (check-in deadline, Form Items scope), and CCI14 moved to it, so its forms and scores stay
-- the same and only the group changes. The member row of CCI14 is kept first in branch_group_member_bak_20261117.

CREATE TABLE IF NOT EXISTS integration_portal.branch_group_member_bak_20261117 AS
SELECT *, now() AS saved_at FROM integration_portal.branch_group_member WHERE branch_code = 'CCI14';

INSERT INTO integration_portal.branch_group (code, name, description, sort, checkin_max_per_day, checkin_deadline)
SELECT 'OCT', 'OCT', 'Kopi Calf To Go OCT (template stock opname OCT)', 40, g.checkin_max_per_day, g.checkin_deadline
  FROM integration_portal.branch_group g WHERE g.code = 'CTG'
ON CONFLICT (code) DO NOTHING;

-- the Form Items scope of CTG (the items CTG is not asked) for OCT too
INSERT INTO integration_portal.form_item_scope (form, item_key, group_code, shown, updated_by)
SELECT form, item_key, 'OCT', shown, updated_by FROM integration_portal.form_item_scope WHERE group_code = 'CTG'
ON CONFLICT (form, item_key, group_code) DO NOTHING;

UPDATE integration_portal.branch_group_member m SET group_id = g.id, added_at = now()
  FROM integration_portal.branch_group g
 WHERE g.code = 'OCT' AND m.branch_code = 'CCI14';
