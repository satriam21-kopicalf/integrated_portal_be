-- The guide (Panduan) becomes a page super admins can show or hide per role or per user (User Accounts). Both roles
-- keep seeing it until it is switched off.
INSERT INTO integration_portal.role_page (role, page) VALUES ('user', 'guide'), ('cost_control', 'guide')
ON CONFLICT DO NOTHING;
