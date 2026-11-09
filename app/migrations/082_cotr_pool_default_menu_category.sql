-- 082: Calf On The Road (2026-11-09).
--   * "Pool Default" (DEFAULT): where the employees and the fleet wait while the pools / HUBs are renamed or rebuilt;
--     a pool deleted hands its employees and fleet over to it (app/cotr.py master_delete). Every employee is put there
--     with no fleet, and every fleet too, so the other pools can be renamed or deleted freely.
--   * cotr_menu_category: the categories of the menus, kept by the team (Master → Menu).
INSERT INTO integration_portal.cotr_pool (code, name, kind, city, address, storage, is_active)
VALUES ('DEFAULT', 'Pool Default', 'main', '', '', '{}', true)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.cotr_driver_assign_backup_082 AS
SELECT code, pool_code, fleet_code FROM integration_portal.cotr_driver;
CREATE TABLE IF NOT EXISTS integration_portal.cotr_fleet_pool_backup_082 AS
SELECT code, pool_code FROM integration_portal.cotr_fleet;
UPDATE integration_portal.cotr_driver SET pool_code = 'DEFAULT', fleet_code = NULL, updated_at = now();
UPDATE integration_portal.cotr_fleet SET pool_code = 'DEFAULT', updated_at = now();

CREATE TABLE IF NOT EXISTS integration_portal.cotr_menu_category (
    code       text PRIMARY KEY,
    name       text NOT NULL,
    sort       integer NOT NULL DEFAULT 0,
    is_active  boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);
-- the categories the menus already use
INSERT INTO integration_portal.cotr_menu_category (code, name, sort)
SELECT upper(regexp_replace(category, '[^A-Za-z0-9]+', '-', 'g')), category, row_number() OVER (ORDER BY category)
  FROM (SELECT DISTINCT category FROM integration_portal.cotr_menu WHERE coalesce(category, '') <> '') c
ON CONFLICT (code) DO NOTHING;
