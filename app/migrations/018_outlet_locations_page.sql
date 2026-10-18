-- Outlet locations (kopicalf 06.9 §12.15, 2026-10-18): Cost Control sets the coordinates and check-in radius of every
-- outlet / hub (page cc.locations, kopicalf permission inventory.count.location). The cost_control role opens it.
INSERT INTO integration_portal.role_page (role, page) VALUES ('cost_control', 'cc.locations')
ON CONFLICT DO NOTHING;
