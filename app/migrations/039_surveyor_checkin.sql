-- 039: Surveyor Check In (2026-10-31, user request): the Cost Control surveyors check in at the outlet they visit, the
-- same way as the PICs (selfie with a face, GPS against the outlet's radius, fake GPS signals, management review). The
-- check-ins share integration_portal.pic_checkin, told apart by `kind`; Monitoring → Surveyor Check In shows them.
-- A surveyor may check in at every Kopi Calf branch. The form opens for the surveyor accounts of
-- docs/User_Accounts_Inventory.xlsx (integrated_portal) as a per-user page; Monitoring stays with super admins until
-- ticked for a role or a user (User Accounts → Roles & pages).

ALTER TABLE integration_portal.pic_checkin ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'pic';
ALTER TABLE integration_portal.pic_checkin DROP CONSTRAINT IF EXISTS pic_checkin_kind_check;
ALTER TABLE integration_portal.pic_checkin ADD CONSTRAINT pic_checkin_kind_check CHECK (kind IN ('pic', 'surveyor'));
CREATE INDEX IF NOT EXISTS pic_checkin_kind_idx ON integration_portal.pic_checkin (kind, visited_at DESC);

INSERT INTO integration_portal.user_page (user_id, page, allowed)
    SELECT id, 'form.surveyor-check-in', true FROM integration_portal.user_account
    WHERE username IN ('aditya.juliansyah', 'ahmad.candra', 'ahmad.rosyidin', 'daffa.fairuz', 'dicky.permana',
                       'dimas.erlangga', 'dzulfikar.lisanto', 'eko.prasetyo', 'endrian.nurrakhim', 'fadhil.rinaldi',
                       'farhan.aprillianto', 'farij.makhzumi', 'febrika.agustian', 'gilang.hendrawan', 'juniadi.santosa',
                       'muhammad.akbar', 'muhammad.ilyasa', 'nugraha.saputra', 'ryan.salsabillah')
ON CONFLICT (user_id, page) DO UPDATE SET allowed = true;
