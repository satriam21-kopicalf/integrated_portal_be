-- Stock opname: Cost Control enters / imports branch counts and downloads the reviewed counts (kopicalf D-56).
-- New pages cc.entry and cc.reports; the cost_control role opens them like its other Cost Control pages.
INSERT INTO integration_portal.role_page (role, page) VALUES
    ('cost_control', 'cc.entry'), ('cost_control', 'cc.reports')
ON CONFLICT DO NOTHING;
