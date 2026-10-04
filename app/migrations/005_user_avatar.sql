-- 005: profile pictures for dashboard users.
--
-- The browser crops and resizes the photo (256x256 WebP/JPEG, ~30 KB) before
-- upload; the API checks type, size and file signature. Images live in their
-- own table so user_account rows stay small; avatar_updated_at versions the
-- image URL (/api/avatars/{id}?v=...) so browsers can cache it for good.

ALTER TABLE integration_portal.user_account ADD COLUMN IF NOT EXISTS avatar_updated_at timestamptz;

CREATE TABLE IF NOT EXISTS integration_portal.user_avatar (
    user_id      uuid        PRIMARY KEY REFERENCES integration_portal.user_account (id) ON DELETE CASCADE,
    content_type text        NOT NULL,
    data         bytea       NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_avatar_type CHECK (content_type IN ('image/webp', 'image/jpeg', 'image/png')),
    CONSTRAINT user_avatar_size CHECK (octet_length(data) <= 524288)
);

COMMENT ON TABLE integration_portal.user_avatar IS 'Profile picture per user (max 512 KB, webp/jpeg/png)';
