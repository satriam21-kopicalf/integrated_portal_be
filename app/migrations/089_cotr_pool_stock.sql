-- 089: Product stock of a pool / HUB per day (2026-11-11), as the sheet "Product" of the pool workbooks:
--   Product Awal (opening)  = the physical leftover of the day before (Sisa Aktual Fisik), less the lots past their expiry
--   + Product Transfer       = cups moved in from another pool or else (entered; minus = moved out)
--   + Product Datang         = new cups from the storage (batches received at the pool, or entered)
--   − Product Terjual        = the sales of the drivers of the pool (transactions)
--   − Waste, − Cupping       = of the drivers at check out and of the pool
--   = Sisa Hasil Hitungan (calculated); Sisa Aktual Fisik is counted per production / batch code; Balance = actual − calculated.
-- The first count of a pool is its opening stock (entered once). A production / batch code "SSDD" is the sequence of the
-- day and the day of the month it was made; its expiry is that day + the shelf life of the menu (or the batch's own).
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool_count (
    id         bigserial PRIMARY KEY,
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day        date NOT NULL,                              -- the end of this day
    menu_code  text NOT NULL REFERENCES integration_portal.cotr_menu (code) ON UPDATE CASCADE ON DELETE CASCADE,
    batch_no   text,
    qty        integer NOT NULL CHECK (qty >= 0),
    expires_on date,
    source     text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'sheet')),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_pool_count_pool_day ON integration_portal.cotr_pool_count (pool_code, day);

-- the figures of a day entered by hand (transfer, arrived) or taken from the sheets (every figure of the days before the app)
CREATE TABLE IF NOT EXISTS integration_portal.cotr_pool_move (
    id         bigserial PRIMARY KEY,
    pool_code  text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day        date NOT NULL,
    menu_code  text NOT NULL REFERENCES integration_portal.cotr_menu (code) ON UPDATE CASCADE ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('transfer', 'arrived', 'sold', 'waste', 'cupping')),
    qty        integer NOT NULL,
    note       text,
    source     text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'sheet')),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cotr_pool_move_pool_day ON integration_portal.cotr_pool_move (pool_code, day);

-- the days of the drivers taken from the sheet "Report Armada" of the pool workbooks (before the app was used): per
-- armada and day the cups per menu (B brought, S left, C cupping, W waste, T sold), the QRIS, the cash counted and the
-- operational costs; the Report Armada of the portal shows them where the driver has no shift in the app.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_report_day (
    id               bigserial PRIMARY KEY,
    pool_code        text NOT NULL REFERENCES integration_portal.cotr_pool (code) ON UPDATE CASCADE ON DELETE CASCADE,
    day              date NOT NULL,
    label            text NOT NULL,                        -- as the sheet names it ("BNI 10 DAHLAN")
    driver_code      text REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE SET NULL,
    lines            jsonb NOT NULL DEFAULT '{}',          -- {menu: {b, s, c, w, t}}
    qris             numeric(14,2),
    cash_actual      numeric(14,2),
    operational      numeric(14,2),
    operational_note text,
    source           text NOT NULL DEFAULT 'sheet',
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pool_code, day, label)
);
