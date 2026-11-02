-- 057 (2026-11-02): Sales calculator by menu — items outside the recipe cards. Supplies that no menu records (cable ties,
-- plastic bags, tissue …) are counted per bill, per portion sold or per day of an outlet with sales; an item is a
-- kc_master item (priced by Pricelist items) or a free item with its own unit and price. A recipe card line may also be
-- a free item ("other": name, unit, price) besides raw materials / packaging (kc_master) and WIP.
CREATE TABLE IF NOT EXISTS integration_portal.cc_extra_item (
    id         serial        PRIMARY KEY,
    item_code  text,                                   -- kc_master item, or null for a free item
    name       text          NOT NULL,
    unit       text          NOT NULL,
    price      numeric(18,4) CHECK (price >= 0),        -- a free item: Rp per unit (a kc_master item uses the pricelist)
    basis      text          NOT NULL CHECK (basis IN ('bill', 'portion', 'day')),
    qty        numeric(18,4) NOT NULL CHECK (qty > 0),   -- per bill / per portion / per outlet-day
    note       text,
    is_active  boolean       NOT NULL DEFAULT true,
    updated_by text,
    updated_at timestamptz   NOT NULL DEFAULT now()
);
