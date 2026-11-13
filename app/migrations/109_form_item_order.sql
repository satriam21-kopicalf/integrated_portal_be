-- 109: the order of the items of a section of Form Product / Outlet (Branch Groups → Form Items, drag and drop,
-- 2026-11-13; app/form_items.py): the item keys of the section in their order; the items are numbered after it.
CREATE TABLE IF NOT EXISTS integration_portal.form_item_order (
    form text NOT NULL,
    section_key text NOT NULL,
    keys text[] NOT NULL,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (form, section_key)
);
