-- 086: Sugar in grams per menu (2026-11-10, management): the aren of a sugar choice (No / Less / Normal / Extra Sugar)
-- set in grams for each recipe card, instead of a % of the card for every menu. A row = the total sugar of ONE portion
-- with that choice on that card (the card itself is the Normal portion). Without a row the % rule of cc_option_rule
-- still applies. Filled first from the ESB BOMs "<menu> (<choice> Sugar)" when plausible, else from the % rule;
-- Cost Control adjusts them (source manual), a refill never overwrites a manual row.
CREATE TABLE IF NOT EXISTS integration_portal.cc_option_card_rule (
    name_key    text          NOT NULL,                 -- the choice, as cc_option_rule.name_key ('less sugar')
    recipe_code text          NOT NULL,                 -- the card (cc_menu_recipe.code)
    grams       numeric(12,3) NOT NULL CHECK (grams >= 0),
    source      text          NOT NULL DEFAULT 'manual' CHECK (source IN ('esb', 'pct', 'check', 'manual')),
    note        text,
    updated_by  text,
    updated_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (name_key, recipe_code)
);
