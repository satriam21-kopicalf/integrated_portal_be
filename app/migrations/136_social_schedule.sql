-- 136: the schedule of Social Media Analytics (2026-11-23, the user: "scheduler yang terstruktur, agar data selalu
-- terupdate"): one row per source — on/off, every how many hours, the last and the next run, and the backfill from a
-- date (the first collection reaches back to 1 January 2026). app/social.py checks every 5 minutes which source is due.
CREATE TABLE IF NOT EXISTS integration_portal.social_schedule (
    source        text PRIMARY KEY,
    enabled       boolean NOT NULL DEFAULT true,
    every_hours   numeric(5,2) NOT NULL CHECK (every_hours >= 1),
    last_run_at   timestamptz,
    next_run_at   timestamptz NOT NULL DEFAULT now(),
    backfill_from date,                        -- set: the next run of the source reaches back to this date, then clears it
    updated_by    text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO integration_portal.social_schedule (source, every_hours, backfill_from) VALUES
    ('news', 3, DATE '2026-01-01'), ('youtube', 6, DATE '2026-01-01'), ('tiktok', 12, DATE '2026-01-01')
ON CONFLICT (source) DO NOTHING;
ALTER TABLE integration_portal.social_run ADD COLUMN IF NOT EXISTS backfill_from date;
