-- 127: Fund Request → the names of the outlet kinds set by management (2026-11-20): Weekly Petty Cash, Marketing & Promo
-- … can be renamed in Monitoring Fund Request → Approvals & Plafon; no row = the default name.
CREATE TABLE IF NOT EXISTS integration_portal.fund_kind_label (
    kind        text        PRIMARY KEY,
    label       text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
