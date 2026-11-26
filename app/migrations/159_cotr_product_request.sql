-- 159: Calf On The Road (2026-11-26): the driver asks the products to bring in the driver app before the shift; the storage
-- sees the request beside the driver in Storage & Assignment and takes it as the assignment (the quantities may change).
-- One request per driver and day, sent again = replaced while it is not assigned.
CREATE TABLE IF NOT EXISTS integration_portal.cotr_product_request (
    day         date        NOT NULL,
    driver_code text        NOT NULL,
    pool_code   text,
    lines       jsonb       NOT NULL,
    note        text,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, driver_code)
);
