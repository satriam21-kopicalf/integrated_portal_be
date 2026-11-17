-- 119: the menus assigned to the recipe cards are cleared (2026-11-17, Cost Control request): every card is made, the
-- team maps the ESB POS menus again from the start. The cards themselves stay. The 102 assignments of today are kept
-- first in cc_menu_recipe_map_bak_20261117 (restore: INSERT … SELECT menu_id, menu_name, recipe_code, updated_by,
-- updated_at FROM the backup). Until the menus are mapped again the Sales Calculator lists them as without a card.
CREATE TABLE IF NOT EXISTS integration_portal.cc_menu_recipe_map_bak_20261117 AS
SELECT *, now() AS saved_at FROM integration_portal.cc_menu_recipe_map;

DELETE FROM integration_portal.cc_menu_recipe_map m
 WHERE EXISTS (SELECT 1 FROM integration_portal.cc_menu_recipe_map_bak_20261117 b WHERE b.menu_id = m.menu_id);
