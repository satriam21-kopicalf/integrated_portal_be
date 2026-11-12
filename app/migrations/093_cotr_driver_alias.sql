-- 093: the names the pool workbooks use for an armada / driver ("BRI 55 BAKRI") tied once to the employee of Master Driver
-- (recommendation #11, 2026-11-12): the import matches them first, the rows already imported take the driver. A name that
-- is not a driver ("BNI 47 PESENAN / CUSTOMER POOL") is marked ignored so it stops asking.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_driver_alias (
    pool_code   text NOT NULL,
    label_key   text NOT NULL,          -- the name without bank / armada number, letters only (cotr_ledger._label_key)
    label       text NOT NULL,          -- as last seen in a workbook
    driver_code text REFERENCES integration_portal.cotr_driver (code) ON UPDATE CASCADE ON DELETE CASCADE,
    ignored     boolean NOT NULL DEFAULT false,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pool_code, label_key),
    CHECK (ignored OR driver_code IS NOT NULL)
);
