-- 138: Social Media Analytics — what is said in a video (2026-11-24): the speech of a TikTok video transcribed on our own
-- VPS (faster-whisper, no third-party service). The transcript is shown as the description and counts for the keyword
-- labels and the sentiment; a video whose audio could not be read keeps the reason, and is not tried again.
ALTER TABLE integration_portal.social_post ADD COLUMN IF NOT EXISTS transcript text;
ALTER TABLE integration_portal.social_post ADD COLUMN IF NOT EXISTS transcribed_at timestamptz;
ALTER TABLE integration_portal.social_post ADD COLUMN IF NOT EXISTS transcript_error text;
