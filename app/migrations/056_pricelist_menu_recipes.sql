-- 056 (2026-11-02): Cost Control — Pricelist Items (bahan baku) and Sales Calculator by Menu.
-- cc_item_price: the price of a raw material (kc_master.item, per base unit) from a date; the latest price valid on a
--   day is the price of that day (history kept). Page cc.pricelist.
-- cc_menu_recipe: a recipe card per menu as the production calculator (premix): its lines are raw materials (items, in
--   their base unit) or WIP / premix recipes (kc_recipe, in grams, exploded into their ingredients); one card serves
--   one or more ESB POS menus (cc_menu_recipe_map: the menus assigned to the card, by ESB menu id). Page cc.sales-calc:
--   material required planning, usage actual (ESB, agg_cost_item_period) vs system (sales × cards), COGS bahan baku.
CREATE TABLE IF NOT EXISTS integration_portal.cc_item_price (
    item_code  text          NOT NULL,
    valid_from date          NOT NULL,
    price      numeric(18,4) NOT NULL CHECK (price >= 0),   -- Rp per base unit of the item
    note       text,
    updated_by text,
    updated_at timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (item_code, valid_from)
);

CREATE TABLE IF NOT EXISTS integration_portal.cc_menu_recipe (
    code       text        PRIMARY KEY,
    name       text        NOT NULL,
    category   text,
    lines      jsonb       NOT NULL DEFAULT '[]',   -- [{"type": "item" | "wip", "code", "name", "qty", "unit"}] per portion
    note       text,
    is_active  boolean     NOT NULL DEFAULT true,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration_portal.cc_menu_recipe_map (
    menu_id     text        PRIMARY KEY,               -- ESB POS menu id (agg_menu_daily.menu_id)
    menu_name   text        NOT NULL,
    recipe_code text        NOT NULL REFERENCES integration_portal.cc_menu_recipe (code) ON UPDATE CASCADE ON DELETE CASCADE,
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_menu_recipe_map_recipe ON integration_portal.cc_menu_recipe_map (recipe_code);

-- the two pages for the roles that open the Production calculator
INSERT INTO integration_portal.role_page (role, page)
SELECT r.role, p.page FROM integration_portal.role_page r CROSS JOIN (VALUES ('cc.pricelist'), ('cc.sales-calc')) p (page)
 WHERE r.page = 'cc.premix'
ON CONFLICT DO NOTHING;
