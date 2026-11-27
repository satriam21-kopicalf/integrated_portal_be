-- 167: Aggregator Insight complaints & reviews (2026-11-27, Aggregator team): the proof and the details of the handling.
-- A ticket (a complaint, or a low review made a ticket) keeps what was done (action), why it happened (root cause), whether
-- and how the customer was contacted, the reply given to the review, who bears the compensation and the ESB bill it is
-- about; its evidence (screenshots of the chat, the refund / voucher, the reply, photos) and every step of its handling.
-- A ticket is done with its resolution and at least one evidence (app/aggregator_quality.py).
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS action        text;  -- REFUND | REPLACE | VOUCHER | APOLOGY | REPLY | OTHER
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS root_cause    text;  -- OUTLET | DRIVER | PLATFORM | CUSTOMER | UNKNOWN
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS contacted     boolean;
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS contact_via   text;  -- CHAT | PHONE | WHATSAPP | REVIEW_REPLY
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS reply_text    text;
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS comp_bearer   text;  -- OUTLET | COMPANY | PLATFORM
ALTER TABLE integration_portal.agg_complaint ADD COLUMN IF NOT EXISTS esb_bill      text;

CREATE TABLE IF NOT EXISTS integration_portal.agg_complaint_evidence (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id bigint NOT NULL REFERENCES integration_portal.agg_complaint(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('CHAT', 'REFUND', 'VOUCHER', 'REPLY', 'PHOTO', 'OTHER')),
    path         text NOT NULL,
    mime         text NOT NULL,
    bytes        integer NOT NULL,
    sha256       text NOT NULL,
    file_name    text,
    note         text,
    created_by   uuid,
    created_by_name text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_complaint_evidence_c ON integration_portal.agg_complaint_evidence (complaint_id);

CREATE TABLE IF NOT EXISTS integration_portal.agg_complaint_event (
    id           bigserial PRIMARY KEY,
    complaint_id bigint NOT NULL REFERENCES integration_portal.agg_complaint(id) ON DELETE CASCADE,
    kind         text NOT NULL,                 -- CREATE | STATUS | EDIT | EVIDENCE | EVIDENCE_REMOVED
    from_status  text,
    to_status    text,
    note         text,
    by_name      text,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agg_complaint_event_c ON integration_portal.agg_complaint_event (complaint_id, at);
