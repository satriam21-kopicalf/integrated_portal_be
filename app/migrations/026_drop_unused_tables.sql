-- 026: tables nothing reads any more (audit 2026-10-21, user decision); JSON backup on the VPS in
-- /root/kc-backups/portal-unused-tables-20261021-092044.json
--   agg_cost_period, agg_cost_item_period, cost_settings: the earlier COGS & usage engine (app/cost_control.py, removed;
--     the page is being rebuilt on the Outlet ratio figures)
--   pic_checkin_backup_20261019: one-off copy made by the PIC Check In reset (022)

DROP TABLE IF EXISTS integration_portal.agg_cost_item_period;
DROP TABLE IF EXISTS integration_portal.agg_cost_period;
DROP TABLE IF EXISTS integration_portal.cost_settings;
DROP TABLE IF EXISTS integration_portal.pic_checkin_backup_20261019;
