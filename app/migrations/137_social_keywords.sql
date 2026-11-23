-- 137: the keywords of Social Media Analytics (2026-11-23): besides Kopi Calf, Calf Coffee Industry, Calf On The Road,
-- Calf To Go, the founders (Danang Yuda Prawira, Ridzky Novasandro), Founder / Co-Founder Kopi Calf and Teman Calf. A post is
-- kept once (platform + id) whatever keyword found it; `keywords` labels every keyword its text names. YouTube and Google
-- News reach back to 1 January again for the new keywords.
ALTER TABLE integration_portal.social_post ADD COLUMN IF NOT EXISTS keywords text[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS social_post_keywords ON integration_portal.social_post USING gin (keywords);
UPDATE integration_portal.social_schedule SET backfill_from = DATE '2026-01-01', next_run_at = now() WHERE source IN ('news', 'youtube');
