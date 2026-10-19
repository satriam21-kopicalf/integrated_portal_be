-- 020: Rating & Review → Google Maps (2026-10-19). The Google place of each Kopi Calf outlet (Places API, app/gmaps.py):
-- the place id is kept for good; data = the latest Place Details (rating, reviews, hours…), replaced at every sync.

CREATE TABLE IF NOT EXISTS integration_portal.gmaps_place (
    branch_code       text          PRIMARY KEY,
    branch_name       text          NOT NULL,
    place_id          text,
    status            text          NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'auto', 'review', 'missing', 'manual', 'none')),
    match_distance_m  integer,                      -- place vs the outlet's coordinates (Lokasi Outlet)
    set_by            text,                         -- username of a manual choice
    matched_at        timestamptz,
    data              jsonb,
    fetched_at        timestamptz,
    error             text,
    updated_at        timestamptz   NOT NULL DEFAULT now()
);
