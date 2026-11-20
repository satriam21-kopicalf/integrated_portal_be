-- 126: Calf On The Road → Master (2026-11-20): every tab of the Master page has its own page in User Accounts. Area
-- followed Pool / HUB and Requests followed Monitoring Armada - Driver; they get the pages cotr.areas and cotr.requests.
-- Whoever opens Pool / HUB keeps Area and whoever opens Monitoring Armada keeps Requests (roles and user exceptions), so
-- no account loses a tab.

INSERT INTO integration_portal.role_page (role, page)
SELECT role, 'cotr.areas' FROM integration_portal.role_page WHERE page = 'cotr.pools'
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.role_page (role, page)
SELECT role, 'cotr.requests' FROM integration_portal.role_page WHERE page = 'cotr.check-in'
ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'cotr.areas', allowed FROM integration_portal.user_page WHERE page = 'cotr.pools'
ON CONFLICT (user_id, page) DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'cotr.requests', allowed FROM integration_portal.user_page WHERE page = 'cotr.check-in'
ON CONFLICT (user_id, page) DO NOTHING;
