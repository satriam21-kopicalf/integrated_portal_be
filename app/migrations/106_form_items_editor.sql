-- 106: Form Items editor of Product & Outlet (Operations → Branch Groups → Form Items, 2026-11-13; app/form_items.py).
-- Operations changes the items of the code (category, note / photo required / optional / none, required, active, label
-- and description ID / EN — a NULL column keeps the code's value), adds items to a section, and sets the weight of a
-- category in the score (Kritikal 3, Mayor 2, Minor 1, others 1).
CREATE TABLE IF NOT EXISTS integration_portal.form_item_override (
    form text NOT NULL,
    item_key text NOT NULL,
    category text,
    note text CHECK (note IN ('required', 'optional', 'none')),
    photo text CHECK (photo IN ('required', 'optional', 'none')),
    required boolean,
    active boolean,
    label text,
    label_en text,
    detail text,
    detail_en text,
    standard text,
    standard_en text,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (form, item_key)
);

CREATE TABLE IF NOT EXISTS integration_portal.form_item_custom (
    form text NOT NULL,
    key text NOT NULL,
    group_key text NOT NULL,
    section_key text NOT NULL,
    sort integer NOT NULL DEFAULT 0,
    label text NOT NULL,
    label_en text,
    category text,
    note text NOT NULL DEFAULT 'required' CHECK (note IN ('required', 'optional', 'none')),
    photo text NOT NULL DEFAULT 'required' CHECK (photo IN ('required', 'optional', 'none')),
    required boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    detail text,
    detail_en text,
    standard text,
    standard_en text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    updated_at timestamptz,
    PRIMARY KEY (form, key)
);

CREATE TABLE IF NOT EXISTS integration_portal.form_category_weight (
    category text PRIMARY KEY,
    weight numeric(6, 2) NOT NULL CHECK (weight BETWEEN 0 AND 100),
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO integration_portal.form_category_weight (category, weight)
VALUES ('Kritikal', 3), ('Mayor', 2), ('Minor', 1)
ON CONFLICT (category) DO NOTHING;
