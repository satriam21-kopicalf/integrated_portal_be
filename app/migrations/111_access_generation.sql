-- 111: one counter raised on every change of access (role pages, page exceptions, menus per platform, accounts),
-- 2026-11-15. The backend runs several workers with in-memory caches; each worker reads this counter (at most every
-- 2 s, app/access.py fresh) and drops its caches when it moved, so a saved setting applies at once on every worker.
CREATE TABLE IF NOT EXISTS integration_portal.access_generation (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    gen bigint NOT NULL DEFAULT 0,
    changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO integration_portal.access_generation (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
