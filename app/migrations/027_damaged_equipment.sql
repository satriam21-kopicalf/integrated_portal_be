-- 027: Form & Monitoring Damaged Equipment (2026-10-21, app/damaged_equipment.py). A PIC requests a repair (building &
-- tools) or a procurement (decoration & tools) for one of their branches; the Maintenance / procurement team takes it
-- through pending → validation → review → accepted → in progress → done (on hold / rejected with a note), assigns and
-- swaps technicians; every step is an event, so the time of every stage is known. Media (photos, one video) are files
-- on the backend volume (MEDIA_DIR), not in the database. Replaces the "Tools & Heavy Tools" skeleton pages.

CREATE TABLE IF NOT EXISTS integration_portal.de_category (
    id               serial        PRIMARY KEY,
    grp              text          NOT NULL,
    name             text          NOT NULL UNIQUE,
    default_urgency  text          NOT NULL DEFAULT 'NORMAL'
                     CHECK (default_urgency IN ('CRITICAL', 'HIGH', 'NORMAL', 'LOW', 'PLANNED')),
    examples         text[]        NOT NULL DEFAULT '{}',
    sort             integer       NOT NULL DEFAULT 0,
    is_active        boolean       NOT NULL DEFAULT true,
    updated_at       timestamptz   NOT NULL DEFAULT now()
);

-- the 6 groups agreed with the user (coffee chain maintenance), default urgency per category
INSERT INTO integration_portal.de_category (grp, name, default_urgency, examples, sort) VALUES
  ('Peralatan Bar & Dapur', 'Mesin Kopi & Grinder', 'CRITICAL', '{Mesin espresso,Grinder,Brewer,Water boiler}', 10),
  ('Peralatan Bar & Dapur', 'Refrigerasi', 'CRITICAL', '{Chiller,Freezer,Showcase,Ice maker,Under-counter fridge}', 11),
  ('Peralatan Bar & Dapur', 'Heavy Tools', 'HIGH', '{Blender,Oven,Microwave,Steamer,Dishwasher,Mesin cuci}', 12),
  ('Peralatan Bar & Dapur', 'Light Tools', 'NORMAL', '{Pitcher,Tamper,Timbangan,Shaker,Alat masak kecil}', 13),
  ('Utilitas', 'Kelistrikan', 'CRITICAL', '{Panel MCB,Stop kontak,Lampu,Kabel,Genset}', 20),
  ('Utilitas', 'Air', 'CRITICAL', '{Pompa,Pipa,Toren,Filter air / RO}', 21),
  ('Utilitas', 'Sanitasi', 'HIGH', '{Wastafel,Toilet,Saluran,Grease trap}', 22),
  ('Utilitas', 'AC & Ventilasi', 'HIGH', '{AC,Exhaust kitchen,Kipas}', 23),
  ('Utilitas', 'Gas', 'CRITICAL', '{Regulator,Selang,Kompor}', 24),
  ('Bangunan', 'Kerusakan Bangunan', 'NORMAL', '{Dinding retak,Lantai,Keramik,Pintu,Jendela,Kaca}', 30),
  ('Bangunan', 'Atap, Plafon & Kanopi', 'HIGH', '{Atap bocor,Plafon,Talang,Kanopi}', 31),
  ('Bangunan', 'Rolling Door', 'CRITICAL', '{Rolling door macet,Rolling door rusak}', 32),
  ('Bangunan', 'Cat & Finishing', 'LOW', '{Cat ulang,Coating,Touch-up}', 33),
  ('Interior & Brand', 'Furniture', 'NORMAL', '{Meja,Kursi,Sofa,Rak,Bar counter,Meja kasir}', 40),
  ('Interior & Brand', 'Neon Box & Signage', 'NORMAL', '{Neon box,Signage,Huruf timbul,Menu board}', 41),
  ('Interior & Brand', 'Sticker & Branding', 'LOW', '{Sticker blur kaca,Wall branding,Sticker menu}', 42),
  ('Interior & Brand', 'Dekorasi & Tanaman', 'LOW', '{Tanaman,Pot,Hiasan,Wall decor}', 43),
  ('Keamanan & IT', 'CCTV & Keamanan', 'HIGH', '{CCTV,DVR,Alarm,Kunci,APAR / alat pemadam}', 50),
  ('Keamanan & IT', 'IT, POS & Jaringan', 'CRITICAL', '{Mesin POS,Printer struk,WiFi,Router,Sound system}', 51),
  ('Proyek & Lainnya', 'Renovasi & Desain', 'PLANNED', '{Renovasi,Perubahan layout,Desain outlet baru}', 60),
  ('Proyek & Lainnya', 'Pest Control', 'HIGH', '{Tikus,Kecoa,Semut,Rayap}', 61),
  ('Proyek & Lainnya', 'Lainnya', 'NORMAL', '{}', 99)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS integration_portal.de_request (
    id               uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    number           text          UNIQUE,                      -- DE-YYMM-NNNN, given at submit
    kind             text          NOT NULL CHECK (kind IN ('REPAIR', 'PROCUREMENT')),
    status           text          NOT NULL DEFAULT 'DRAFT' CHECK (status IN
                     ('DRAFT', 'PENDING', 'VALIDATION', 'REVIEW', 'ON_HOLD', 'ACCEPTED', 'IN_PROGRESS', 'DONE', 'REJECTED')),
    hold_from        text,                                      -- status to resume after ON_HOLD
    user_id          uuid          NOT NULL,
    username         text          NOT NULL,
    full_name        text,
    branch_code      text          NOT NULL,
    branch_name      text          NOT NULL,
    analysis         text,                                      -- analysis of the damage / reason of the procurement
    cause            text,                                      -- WEAR | MISUSE | AGE | OTHER (repairs)
    urgency          text          NOT NULL DEFAULT 'NORMAL',
    latitude         numeric(9, 6),
    longitude        numeric(9, 6),
    accuracy_m       numeric,
    distance_m       integer,
    outside_radius   boolean       NOT NULL DEFAULT false,
    signature        bytea,
    signature_type   text,
    signed_name      text,
    signed_at        timestamptz,
    share_token      text          UNIQUE,
    share_expires_at timestamptz,
    est_cost         numeric(14, 2),
    actual_cost      numeric(14, 2),
    vendor           text,
    duplicate_of     uuid          REFERENCES integration_portal.de_request (id),
    created_at       timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now(),
    submitted_at     timestamptz,
    closed_at        timestamptz                                -- done or rejected
);
CREATE INDEX IF NOT EXISTS de_request_status ON integration_portal.de_request (status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS de_request_branch ON integration_portal.de_request (branch_code, status);
CREATE INDEX IF NOT EXISTS de_request_user ON integration_portal.de_request (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.de_item (
    id               serial        PRIMARY KEY,
    request_id       uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    category_id      integer       NOT NULL REFERENCES integration_portal.de_category (id),
    name             text          NOT NULL,
    qty              integer       NOT NULL DEFAULT 1 CHECK (qty BETWEEN 1 AND 9999),
    location         text,
    urgency          text          NOT NULL CHECK (urgency IN ('CRITICAL', 'HIGH', 'NORMAL', 'LOW', 'PLANNED')),
    note             text,
    sort             integer       NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS de_item_request ON integration_portal.de_item (request_id);

CREATE TABLE IF NOT EXISTS integration_portal.de_media (
    id               uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id       uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    kind             text          NOT NULL CHECK (kind IN ('PHOTO', 'VIDEO', 'AFTER')),  -- AFTER = work done (team)
    storage_path     text          NOT NULL,
    mime             text          NOT NULL,
    bytes            integer       NOT NULL,
    sha256           text          NOT NULL,
    file_name        text,
    taken_at         timestamptz,
    latitude         numeric(9, 6),
    longitude        numeric(9, 6),
    uploaded_by      uuid          NOT NULL,
    uploaded_by_name text,
    uploaded_at      timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS de_media_request ON integration_portal.de_media (request_id, kind);

-- every step of a request: the timeline shown to the PIC and the time tracker of the management
CREATE TABLE IF NOT EXISTS integration_portal.de_event (
    id               bigserial     PRIMARY KEY,
    request_id       uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    at               timestamptz   NOT NULL DEFAULT now(),
    actor_id         uuid,
    actor_name       text,
    kind             text          NOT NULL CHECK (kind IN ('SUBMIT', 'STATUS', 'ASSIGN', 'RELEASE', 'SWAP', 'COST', 'NOTE', 'DUPLICATE')),
    from_status      text,
    to_status        text,
    note             text,
    details          jsonb         NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS de_event_request ON integration_portal.de_event (request_id, at);

CREATE TABLE IF NOT EXISTS integration_portal.de_technician (
    id               serial        PRIMARY KEY,
    name             text          NOT NULL,
    kind             text          NOT NULL DEFAULT 'INTERNAL' CHECK (kind IN ('INTERNAL', 'VENDOR')),
    company          text,
    skills           text[]        NOT NULL DEFAULT '{}',       -- category groups
    city             text,
    phone            text,
    notes            text,
    is_active        boolean       NOT NULL DEFAULT true,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integration_portal.de_assignment (
    id               serial        PRIMARY KEY,
    request_id       uuid          NOT NULL REFERENCES integration_portal.de_request (id) ON DELETE CASCADE,
    technician_id    integer       NOT NULL REFERENCES integration_portal.de_technician (id),
    scheduled_for    date,
    status           text          NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'RELEASED', 'DONE')),
    note             text,
    assigned_at      timestamptz   NOT NULL DEFAULT now(),
    assigned_by      text,
    released_at      timestamptz,
    released_note    text
);
CREATE INDEX IF NOT EXISTS de_assignment_request ON integration_portal.de_assignment (request_id, status);
CREATE INDEX IF NOT EXISTS de_assignment_technician ON integration_portal.de_assignment (technician_id, status);

CREATE TABLE IF NOT EXISTS integration_portal.de_counter (
    month            text          PRIMARY KEY,                 -- YYMM
    n                integer       NOT NULL
);

-- the "Tools & Heavy Tools" skeleton pages become Damaged Equipment (same people keep their access)
UPDATE integration_portal.role_page SET page = 'form.damaged-equipment' WHERE page = 'form.tools'
  AND NOT EXISTS (SELECT 1 FROM integration_portal.role_page r2 WHERE r2.role = role_page.role AND r2.page = 'form.damaged-equipment');
UPDATE integration_portal.role_page SET page = 'mon.damaged-equipment' WHERE page = 'mon.tools'
  AND NOT EXISTS (SELECT 1 FROM integration_portal.role_page r2 WHERE r2.role = role_page.role AND r2.page = 'mon.damaged-equipment');
UPDATE integration_portal.user_page SET page = 'form.damaged-equipment' WHERE page = 'form.tools'
  AND NOT EXISTS (SELECT 1 FROM integration_portal.user_page u2 WHERE u2.user_id = user_page.user_id AND u2.page = 'form.damaged-equipment');
UPDATE integration_portal.user_page SET page = 'mon.damaged-equipment' WHERE page = 'mon.tools'
  AND NOT EXISTS (SELECT 1 FROM integration_portal.user_page u2 WHERE u2.user_id = user_page.user_id AND u2.page = 'mon.damaged-equipment');
DELETE FROM integration_portal.role_page WHERE page IN ('form.tools', 'mon.tools');
DELETE FROM integration_portal.user_page WHERE page IN ('form.tools', 'mon.tools');

-- role user (the PICs) gets the form like the other forms; accounts that were kept off the forms (the branch accounts,
-- form.outlet switched off) stay off this one too
INSERT INTO integration_portal.role_page (role, page) VALUES ('user', 'form.damaged-equipment') ON CONFLICT DO NOTHING;
INSERT INTO integration_portal.user_page (user_id, page, allowed)
SELECT user_id, 'form.damaged-equipment', false FROM integration_portal.user_page WHERE page = 'form.outlet' AND allowed = false
ON CONFLICT (user_id, page) DO NOTHING;
