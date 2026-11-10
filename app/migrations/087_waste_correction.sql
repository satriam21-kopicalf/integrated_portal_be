-- 087: Cost Control corrects a waste entry (2026-11-10): the quantity, item, category, dates or note of an entry fixed
-- in place (the figures, values and running stock follow), every change kept with its old and new value and the reason.
CREATE TABLE IF NOT EXISTS integration_portal.waste_correction (
    id          bigserial   PRIMARY KEY,
    entry_id    uuid        NOT NULL REFERENCES integration_portal.waste_entry (id) ON DELETE CASCADE,
    field       text        NOT NULL,   -- qty | item | category | businessDate | productionDate | expiredDate | note
    old_value   text,
    new_value   text,
    reason      text        NOT NULL,
    by_name     text        NOT NULL,
    by_user     uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS waste_correction_entry_idx ON integration_portal.waste_correction (entry_id, created_at);
