-- 085: Items of the Product / Outlet forms per branch (2026-11-10, Operations → Branch Groups → Items): on top of the
-- group (CTG / CTGP / CCI, form_item_scope of 069) an item may be shown at one outlet whose group is not asked it, or
-- hidden at one whose group is. The branch comes first, then the group (app/form_scope.py, app/visit_forms.applies).
CREATE TABLE IF NOT EXISTS integration_portal.form_item_branch_scope (
    form        text        NOT NULL CHECK (form IN ('product', 'outlet')),
    item_key    text        NOT NULL,
    branch_code text        NOT NULL,
    shown       boolean     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    PRIMARY KEY (form, item_key, branch_code)
);
