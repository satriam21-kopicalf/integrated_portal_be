-- 103: the cost group of an ESB category with a valid-from date and its history (Cost Control · Outlet Ratio basis GR,
-- 2026-11-13; app/ratio.py). Management and Cost Control agree on the groups; a change applies from its date on and the
-- months before keep the group they had (no silent change of a closed month). cost_group NULL = back to the supplier.
-- The rows of cc_cost_group (migration 102, a few hours old) become the first versions.
CREATE TABLE IF NOT EXISTS integration_portal.cc_cost_group_version (
    id bigserial PRIMARY KEY,
    category text NOT NULL,
    valid_from date NOT NULL,
    cost_group text CHECK (cost_group IN ('bb', 'espresso', 'dimsum', 'gs', 'other')),
    note text,
    updated_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (category, valid_from)
);

INSERT INTO integration_portal.cc_cost_group_version (category, valid_from, cost_group, note, updated_by, created_at)
SELECT category, date '2026-11-01', cost_group, 'Pemetaan awal (sebelum riwayat)', updated_by, updated_at
  FROM integration_portal.cc_cost_group
ON CONFLICT (category, valid_from) DO NOTHING;
