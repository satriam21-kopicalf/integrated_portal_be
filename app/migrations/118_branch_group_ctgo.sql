-- 118: the Branch Group of CCI14 BTN Senayan City is named CTGO, as its stock opname template "Template SO CTGO"
-- (kc_inventory TPL-OCT) (2026-11-17, user). 116 created it as OCT: the code, the name and its Form Items scope rows
-- are renamed; members and settings stay.
UPDATE integration_portal.form_item_scope SET group_code = 'CTGO' WHERE group_code = 'OCT';
UPDATE integration_portal.branch_group
   SET code = 'CTGO', name = 'CTGO', description = 'Kopi Calf To Go CTGO (Template SO CTGO)', updated_at = now()
 WHERE code = 'OCT' AND NOT EXISTS (SELECT 1 FROM integration_portal.branch_group WHERE code = 'CTGO');
