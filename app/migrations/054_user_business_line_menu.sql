-- 054 (2026-11-02): User Accounts per business line, and the menu display of an account.
-- business_line: the line an account belongs to (tabs of User Accounts, branches of that line only): kopicalf (ESB
-- company CALF), wasgee (CALF7), cotr (Calf On The Road: drivers and the COTR team, pools instead of ESB branches).
-- hidden_pages: pages left out of the sidebar of the account (a superadmin keeps a short menu); display only, the
-- access stays as the role gives it.
ALTER TABLE integration_portal.user_account
    ADD COLUMN IF NOT EXISTS business_line text NOT NULL DEFAULT 'kopicalf',
    ADD COLUMN IF NOT EXISTS hidden_pages text[] NOT NULL DEFAULT '{}';

DO $$ BEGIN
    ALTER TABLE integration_portal.user_account
        ADD CONSTRAINT user_account_business_line_check CHECK (business_line IN ('kopicalf', 'wasgee', 'cotr'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- the line of the existing accounts: drivers and accounts with COTR pages -> cotr; accounts whose branches are all of
-- Wasgee Tea (CALF7) -> wasgee; the rest stays kopicalf
UPDATE integration_portal.user_account u SET business_line = 'cotr'
 WHERE u.role = 'driver'
    OR EXISTS (SELECT 1 FROM integration_portal.user_page p WHERE p.user_id = u.id AND p.page LIKE 'cotr.%' AND p.allowed);

UPDATE integration_portal.user_account u SET business_line = 'wasgee'
 WHERE u.business_line = 'kopicalf'
   AND EXISTS (SELECT 1 FROM integration_portal.user_branch x WHERE x.user_id = u.id)
   AND NOT EXISTS (SELECT 1 FROM integration_portal.user_branch x
                    WHERE x.user_id = u.id
                      AND NOT EXISTS (SELECT 1 FROM integration_esb.master_branches b
                                       WHERE b.branch_code = x.branch_code AND b.company_code = 'CALF7'));

CREATE INDEX IF NOT EXISTS user_account_business_line ON integration_portal.user_account (business_line);

-- the platforms a page is shown on (the menus of the portal, Operations and the driver app Calf On The Road), set by
-- the superadmins; no row = the default of each app. Display only: the access to a page stays as the role gives it.
CREATE TABLE IF NOT EXISTS integration_portal.page_platform (
    page       text PRIMARY KEY,
    platforms  text[] NOT NULL DEFAULT '{}' CHECK (platforms <@ ARRAY['portal', 'operations', 'cotr']::text[]),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);
