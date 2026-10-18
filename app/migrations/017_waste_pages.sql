-- Waste (skeleton, 2026-10-18): outlets enter the waste of their branch (page "waste", menu under Stock Opname), Cost
-- Control follows and reviews it (pages cc.waste-monitor, cc.waste-review in the Cost Control menu). The cost_control
-- role opens the three pages; role "user" gets "waste" from User Accounts → Roles & pages once the input is ready.
INSERT INTO integration_portal.role_page (role, page) VALUES
    ('cost_control', 'waste'), ('cost_control', 'cc.waste-monitor'), ('cost_control', 'cc.waste-review')
ON CONFLICT DO NOTHING;
