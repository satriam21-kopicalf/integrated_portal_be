-- 135: Social Media Analytics (2026-11-23): what the public says about Kopi Calf, collected by the portal itself
-- (app/social.py) from the sources open without a login or an API key — YouTube (yt-dlp), Google News RSS, Reddit,
-- the official TikTok account — on our VPS, into our database. No third-party scraping service.
CREATE TABLE IF NOT EXISTS integration_portal.social_post (
    platform    text NOT NULL,
    post_id     text NOT NULL,
    source      text NOT NULL,                 -- search / comment / google-news / account / hashtag (imported)
    keyword     text,
    url         text,
    author      text,
    text        text,
    created_at  timestamptz,
    likes       bigint,
    comments    bigint,
    views       bigint,
    shares      bigint,
    sentiment   text CHECK (sentiment IN ('positive', 'negative', 'neutral')),
    raw         jsonb NOT NULL DEFAULT '{}',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, post_id)
);
CREATE INDEX IF NOT EXISTS social_post_created ON integration_portal.social_post (created_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.social_run (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    found       integer,
    added       integer,
    error       text
);
