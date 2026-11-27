-- 162: Building & Renovation (2026-11-27, Building team; page mon.building): the material catalog and the directory of
-- stores and suppliers, from "Data Base Material_Update_01_09_2026.xlsx" (imported once). Every price has its date and
-- its history (building_price): an estimate shows how old the price is. Projects, RAB and progress come next.
CREATE TABLE IF NOT EXISTS integration_portal.building_vendor (
    id           serial PRIMARY KEY,
    kind         text        NOT NULL CHECK (kind IN ('STORE', 'SUPPLIER')),
    name         text        NOT NULL,
    location     text,
    goods        text,
    contact_name text,
    phone        text,
    maps         text,
    website      text,
    bank         text,
    account_no   text,
    account_name text,
    note         text,
    is_active    boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text
);

CREATE TABLE IF NOT EXISTS integration_portal.building_material (
    id          serial PRIMARY KEY,
    grp         text        NOT NULL,             -- Sipil, MEP, CCTV, Alarm & Internet, Alat Kerja, Furniture, Lighting
    category    text        NOT NULL,             -- ACP, Keramik, Kabel …
    spec        text,                             -- brand / size
    detail      text,                             -- series / model
    unit        text,
    price       numeric(16,2),
    price_on    date,
    vendor_id   integer REFERENCES integration_portal.building_vendor(id) ON DELETE SET NULL,
    vendor_text text,                             -- the store as written when it is not in the directory
    note        text,
    repeat      boolean     NOT NULL DEFAULT false, -- a material ordered again and again (sheet Material Repeat Order)
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
CREATE INDEX IF NOT EXISTS building_material_grp ON integration_portal.building_material (grp, category);

CREATE TABLE IF NOT EXISTS integration_portal.building_price (
    id          serial PRIMARY KEY,
    material_id integer     NOT NULL REFERENCES integration_portal.building_material(id) ON DELETE CASCADE,
    price       numeric(16,2) NOT NULL,
    price_on    date        NOT NULL,
    vendor_id   integer REFERENCES integration_portal.building_vendor(id) ON DELETE SET NULL,
    source      text        NOT NULL DEFAULT 'MANUAL', -- IMPORT | MANUAL | INVOICE
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text
);
CREATE INDEX IF NOT EXISTS building_price_material ON integration_portal.building_price (material_id, price_on DESC);
