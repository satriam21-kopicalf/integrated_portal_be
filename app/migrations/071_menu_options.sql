-- 071: Sales Calculator by Menu, variants & modifiers (2026-11-05). A menu sold with a choice (Less / No / Extra Sugar,
-- Extra Shot, Ice Cream …: the ESB packages / extras of the line) uses other materials than its card: the card stays the
-- standard portion (Normal Sugar) and a rule per choice changes it.
--   agg_menu_option_daily  the choices sold per day × branch × parent menu (the menu line they belong to), from the POS
--                          transactions (app/aggregates.py SQL_OPTION); agg_menu_option_done marks the days built
--   cc_option_rule         the rule of a choice, by its name (every "Less Sugar" of every menu): none, or adjustments
--                          [{"target": "sugar" | "item", "code", "mode": "pct" | "qty", "value"}]:
--                            pct  % of what the parent card uses of the target (sugar = every sugar item of the card)
--                            qty  this much of the item (its base unit) per choice sold

CREATE TABLE IF NOT EXISTS integration_portal.agg_menu_option_daily (
    sales_date  date          NOT NULL,
    branch_code text          NOT NULL,
    parent_id   text          NOT NULL,   -- ESB menu id of the line the choice belongs to
    option_id   text          NOT NULL,   -- ESB menu id of the package / extra
    kind        text          NOT NULL,   -- package | extra
    option_name text          NOT NULL,
    qty         numeric(18,3) NOT NULL,
    PRIMARY KEY (sales_date, branch_code, parent_id, option_id, kind)
);
CREATE INDEX IF NOT EXISTS agg_menu_option_daily_name ON integration_portal.agg_menu_option_daily (lower(option_name));

CREATE TABLE IF NOT EXISTS integration_portal.agg_menu_option_done (
    sales_date date        PRIMARY KEY,
    built_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration_portal.cc_option_rule (
    name_key   text        PRIMARY KEY,   -- lower(trim(name))
    name       text        NOT NULL,
    effect     text        NOT NULL CHECK (effect IN ('none', 'adjust')),
    lines      jsonb       NOT NULL DEFAULT '[]',
    note       text,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- the sugar choices of the POS (user decision 2026-11-05: Less −50 %, No −100 %, Extra +50 % of the card's sugar)
INSERT INTO integration_portal.cc_option_rule (name_key, name, effect, lines, note, updated_by) VALUES
    ('normal sugar', 'Normal Sugar', 'none', '[]', 'The card is the Normal Sugar portion', 'system'),
    ('less sugar', 'Less Sugar', 'adjust', '[{"target": "sugar", "mode": "pct", "value": -50}]', 'Half the sugar of the card', 'system'),
    ('no sugar', 'No Sugar', 'adjust', '[{"target": "sugar", "mode": "pct", "value": -100}]', 'No sugar', 'system'),
    ('extra sugar', 'Extra Sugar', 'adjust', '[{"target": "sugar", "mode": "pct", "value": 50}]', 'Half again the sugar of the card', 'system')
ON CONFLICT (name_key) DO NOTHING;
