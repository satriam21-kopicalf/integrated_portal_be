-- 058 (2026-11-02): an ESB menu id is unique within a company only (Kopi Calf menu 1 = Es Kopi Calf Premium; Wasgee Tea
-- has its own menu 1), so the menus of the Sales Calculator by Menu are "<company>:<menu id>". The assignments saved
-- before with the bare id were made from the Kopi Calf list: they become CALF:<id>.
UPDATE integration_portal.cc_menu_recipe_map SET menu_id = 'CALF:' || menu_id WHERE position(':' IN menu_id) = 0;
