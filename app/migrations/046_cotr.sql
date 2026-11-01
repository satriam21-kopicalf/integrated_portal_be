-- 046 (2026-11-01): Calf On The Road (COTR). Drivers sell from a fleet loaded at a pool / HUB and use the driver app
-- (fe-calfontheroad, cotr.kopicalf.co.id); the COTR team configures the masters and follows the shifts in the portal
-- (Monitoring COTR, pages cotr.*). A driver signs in with an account of role "driver" made in User Accounts and tied to
-- a row of cotr_driver; the portal refuses those accounts. The stock on a fleet is not stored: start stock + reloads
-- received - sales not voided (app/cotr.py).
ALTER TABLE integration_portal.user_account DROP CONSTRAINT IF EXISTS user_account_role_check;
ALTER TABLE integration_portal.user_account
    ADD CONSTRAINT user_account_role_check CHECK (role IN ('superadmin', 'cost_control', 'user', 'outlet', 'driver'));

-- masters ---------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('main', 'parent', 'storage')),
    parent_code text REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    city        text NOT NULL DEFAULT '',
    address     text NOT NULL DEFAULT '',
    storage     text[] NOT NULL DEFAULT '{}',          -- what is kept there: menus, raw materials, packaging
    is_active   boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet (
    code         text PRIMARY KEY,
    type         text NOT NULL CHECK (type IN ('motor', 'gerobak', 'sepeda')),
    plate        text,
    pool_code    text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    capacity     integer NOT NULL DEFAULT 0 CHECK (capacity >= 0),   -- cups
    condition    text NOT NULL DEFAULT 'good' CHECK (condition IN ('good', 'service')),
    last_service date,
    is_active    boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   uuid
);

-- a recipe per output: what one cup or one batch uses; a line whose item is the output of another BOM is exploded by it
CREATE TABLE IF NOT EXISTS integration_portal.cotr_bom (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    output     text NOT NULL,
    yield_qty  numeric(14,3) NOT NULL CHECK (yield_qty > 0),
    yield_unit text NOT NULL,
    lines      jsonb NOT NULL DEFAULT '[]',               -- [{item, qty, unit}]
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_menu (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    category   text NOT NULL DEFAULT '',
    price      numeric(14,2) NOT NULL CHECK (price >= 0),
    bom_code   text REFERENCES integration_portal.cotr_bom (code) ON UPDATE CASCADE,
    is_active  boolean NOT NULL DEFAULT true,
    sort       integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_driver (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    phone      text NOT NULL DEFAULT '',
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    fleet_code text REFERENCES integration_portal.cotr_fleet (code) ON UPDATE CASCADE,
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'leave', 'inactive')),
    joined_on  date,
    user_id    uuid UNIQUE REFERENCES integration_portal.user_account (id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

-- the day of a driver ---------------------------------------------------------------------------------------------
-- one shift per driver and day: start check in -> sales, reloads -> end check in (the counted stock and the stock the
-- system expected at that moment)
CREATE TABLE IF NOT EXISTS integration_portal.cotr_shift (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_code    text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE,
    day            date NOT NULL,
    pool_code      text,
    fleet_code     text,
    status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    ended_at       timestamptz,
    start_stock    jsonb NOT NULL DEFAULT '{}',           -- {menu code: cups}
    end_stock      jsonb,
    expected_stock jsonb,
    end_note       text,
    UNIQUE (driver_code, day)
);
CREATE INDEX IF NOT EXISTS cotr_shift_day ON integration_portal.cotr_shift (day);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_check_in (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id    uuid NOT NULL REFERENCES integration_portal.cotr_shift (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('start', 'end')),
    at          timestamptz NOT NULL DEFAULT now(),
    latitude    numeric(9,6),
    longitude   numeric(9,6),
    accuracy_m  numeric(10,1),
    photo       bytea,
    photo_type  text,
    stock       jsonb NOT NULL DEFAULT '{}',
    note        text,
    UNIQUE (shift_id, kind)
);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_sale (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id    uuid NOT NULL REFERENCES integration_portal.cotr_shift (id) ON DELETE CASCADE,
    at          timestamptz NOT NULL DEFAULT now(),
    lines       jsonb NOT NULL,                           -- [{code, name, qty, price}] as sold (price of that moment)
    payment     text NOT NULL CHECK (payment IN ('cash', 'qris')),
    total       numeric(14,2) NOT NULL,
    voided_at   timestamptz,
    void_reason text
);
CREATE INDEX IF NOT EXISTS cotr_sale_shift ON integration_portal.cotr_sale (shift_id);

-- a reload asked by the driver, approved / rejected by the pool (portal) and received by the driver: only a received
-- reload adds to the stock on the fleet
CREATE TABLE IF NOT EXISTS integration_portal.cotr_reload (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id    uuid NOT NULL REFERENCES integration_portal.cotr_shift (id) ON DELETE CASCADE,
    at          timestamptz NOT NULL DEFAULT now(),
    lines       jsonb NOT NULL,                           -- {menu code: cups}
    note        text,
    status      text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'approved', 'rejected', 'received')),
    decided_at  timestamptz,
    decided_by  uuid,
    decision_note text,
    received_at timestamptz
);
CREATE INDEX IF NOT EXISTS cotr_reload_shift ON integration_portal.cotr_reload (shift_id);

-- costs on the road (fuel, ice, repair, parking …) with the receipt, approved or rejected in the portal
CREATE TABLE IF NOT EXISTS integration_portal.cotr_request (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_code   text NOT NULL REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE,
    shift_id      uuid REFERENCES integration_portal.cotr_shift (id) ON DELETE SET NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    kind          text NOT NULL CHECK (kind IN ('fuel', 'ice', 'repair', 'parking', 'other')),
    amount        numeric(14,2) NOT NULL CHECK (amount > 0),
    note          text NOT NULL,
    photo         bytea NOT NULL,
    photo_type    text NOT NULL,
    status        text NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'approved', 'rejected')),
    decided_at    timestamptz,
    decided_by    uuid,
    decision_note text
);
CREATE INDEX IF NOT EXISTS cotr_request_at ON integration_portal.cotr_request (at);

CREATE TABLE IF NOT EXISTS integration_portal.cotr_waste (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    day        date NOT NULL,
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE,
    item       text NOT NULL,
    qty        numeric(14,3) NOT NULL CHECK (qty > 0),
    unit       text NOT NULL,
    reason     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid
);

-- the masters of the prototype (2026-11-01), so the team starts from the same data it tried
INSERT INTO integration_portal.cotr_pool (code, name, kind, parent_code, city, address, storage) VALUES
    ('HUB-BDG', 'HUB COTR Bandung', 'main', NULL, 'Bandung', 'Jl. Supratman 12', '{Menu jadi,Bahan baku,Kemasan}'),
    ('POOL-DAGO', 'Pool Dago', 'parent', 'HUB-BDG', 'Bandung', 'Jl. Ir. H. Juanda 88', '{Menu jadi,Es batu}'),
    ('POOL-BUAH', 'Pool Buah Batu', 'parent', 'HUB-BDG', 'Bandung', 'Jl. Buah Batu 201', '{Menu jadi,Es batu}'),
    ('STR-BDG-01', 'Cold storage HUB Bandung', 'storage', 'HUB-BDG', 'Bandung', 'HUB COTR Bandung', '{Fresh milk,Espresso,Premix}')
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.cotr_fleet (code, type, plate, pool_code, capacity, condition, last_service) VALUES
    ('COTR-M01', 'motor', 'D 4521 ABC', 'POOL-DAGO', 120, 'good', '2026-10-12'),
    ('COTR-M02', 'motor', 'D 3310 KCF', 'POOL-DAGO', 120, 'good', '2026-10-20'),
    ('COTR-M03', 'motor', 'D 6677 CTR', 'POOL-BUAH', 120, 'service', '2026-09-02'),
    ('COTR-G01', 'gerobak', NULL, 'POOL-BUAH', 200, 'good', '2026-10-01'),
    ('COTR-S01', 'sepeda', NULL, 'HUB-BDG', 60, 'good', '2026-10-25')
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.cotr_bom (code, name, output, yield_qty, yield_unit, lines) VALUES
    ('BOM-EKC', 'Es Kopi Calf (cup)', 'Es Kopi Calf', 1, 'cup',
     '[{"item": "Premix Es Kopi Calf", "qty": 150, "unit": "g"}, {"item": "Es batu", "qty": 120, "unit": "g"}, {"item": "Cup 16oz + lid", "qty": 1, "unit": "pcs"}]'),
    ('BOM-PRM-EKC', 'Premix Es Kopi Calf (batch)', 'Premix Es Kopi Calf', 5000, 'g',
     '[{"item": "Fresh milk", "qty": 2800, "unit": "g"}, {"item": "Espresso premium", "qty": 1300, "unit": "g"}, {"item": "Creamer", "qty": 520, "unit": "g"}, {"item": "Gula aren", "qty": 380, "unit": "g"}]'),
    ('BOM-ECH', 'Es Coklat (cup)', 'Es Coklat', 1, 'cup',
     '[{"item": "Premix coklat", "qty": 160, "unit": "g"}, {"item": "Es batu", "qty": 120, "unit": "g"}, {"item": "Cup 16oz + lid", "qty": 1, "unit": "pcs"}]'),
    ('BOM-AMR', 'Americano (cup)', 'Americano', 1, 'cup',
     '[{"item": "Espresso premium", "qty": 36, "unit": "g"}, {"item": "Air mineral", "qty": 180, "unit": "g"}, {"item": "Es batu", "qty": 120, "unit": "g"}, {"item": "Cup 16oz + lid", "qty": 1, "unit": "pcs"}]')
ON CONFLICT (code) DO NOTHING;

INSERT INTO integration_portal.cotr_menu (code, name, category, price, bom_code, is_active, sort) VALUES
    ('COTR-EKC', 'Es Kopi Calf', 'Kopi susu', 15000, 'BOM-EKC', true, 1),
    ('COTR-AMR', 'Americano', 'Kopi hitam', 12000, 'BOM-AMR', true, 2),
    ('COTR-ECH', 'Es Coklat', 'Non kopi', 14000, 'BOM-ECH', true, 3),
    ('COTR-MTC', 'Es Matcha', 'Non kopi', 16000, NULL, false, 4)
ON CONFLICT (code) DO NOTHING;

-- the drivers of the prototype, without an account: tie one in Master Driver once its login exists
INSERT INTO integration_portal.cotr_driver (code, name, phone, pool_code, fleet_code, status, joined_on) VALUES
    ('DRV-001', 'Rizki Pratama', '0812-2201-1001', 'POOL-DAGO', 'COTR-M01', 'active', '2026-08-01'),
    ('DRV-002', 'Dimas Saputra', '0812-2201-1002', 'POOL-DAGO', 'COTR-M02', 'active', '2026-08-01'),
    ('DRV-003', 'Agus Hidayat', '0812-2201-1003', 'POOL-BUAH', 'COTR-G01', 'active', '2026-09-15'),
    ('DRV-004', 'Fajar Nugraha', '0812-2201-1004', 'POOL-BUAH', NULL, 'leave', '2026-09-15'),
    ('DRV-005', 'Yoga Permana', '0812-2201-1005', 'HUB-BDG', 'COTR-S01', 'active', '2026-10-10')
ON CONFLICT (code) DO NOTHING;
