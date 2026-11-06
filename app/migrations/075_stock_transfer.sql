-- 075: Monitoring Stock — Stock List and Transfer Stock (TITO, 2026-11-06; app/stock_transfer.py).
-- An outlet (the requester) asks another outlet (the source) for materials: the source checks and accepts (or declines)
-- with a note, Cost Control approves (or rejects), the requester receives with a photo of the goods. Only then the stock
-- of both outlets moves (app/stock_moves.py: transferIn / transferOut), valued with the Pricelist of the day of receipt.
-- The quantity is the one requested (no partial transfer); a source without stock (≤ 0) cannot be asked. Recorded in
-- the portal only: Cost Control labels each transfer whether it goes to ESB later (esb_label).

CREATE TABLE IF NOT EXISTS integration_portal.stock_transfer (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    number            text          NOT NULL UNIQUE,                 -- TS-YYMM-0001
    to_branch         text          NOT NULL,                        -- the requester: the goods come in here
    to_name           text          NOT NULL,
    from_branch       text          NOT NULL,                        -- the source: the goods go out of here
    from_name         text          NOT NULL,
    status            text          NOT NULL DEFAULT 'submitted'
                      CHECK (status IN ('submitted', 'accepted', 'declined', 'approved', 'rejected', 'received', 'cancelled')),
    note              text,
    requested_by      uuid          NOT NULL,
    requested_name    text          NOT NULL,
    requested_at      timestamptz   NOT NULL DEFAULT now(),
    photo             bytea         NOT NULL,
    photo_type        text          NOT NULL,
    signature         bytea         NOT NULL,
    signature_type    text          NOT NULL,
    source_note       text,
    source_by         text,
    source_at         timestamptz,
    cc_note           text,
    cc_by             text,
    cc_at             timestamptz,
    receive_photo     bytea,
    receive_photo_type text,
    receive_note      text,
    received_by       text,
    received_at       timestamptz,
    value             numeric(18,2),                                   -- at receipt: Σ qty base × price
    esb_label         text          CHECK (esb_label IN ('esb', 'portal')),
    esb_label_note    text,
    esb_label_by      text,
    esb_label_at      timestamptz,
    CHECK (to_branch <> from_branch)
);
CREATE INDEX IF NOT EXISTS stock_transfer_to ON integration_portal.stock_transfer (to_branch, requested_at DESC);
CREATE INDEX IF NOT EXISTS stock_transfer_from ON integration_portal.stock_transfer (from_branch, requested_at DESC);
CREATE INDEX IF NOT EXISTS stock_transfer_status ON integration_portal.stock_transfer (status, requested_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.stock_transfer_line (
    transfer_id       uuid          NOT NULL REFERENCES integration_portal.stock_transfer (id) ON DELETE CASCADE,
    line_no           integer       NOT NULL,
    item_code         text          NOT NULL,
    item_name         text          NOT NULL,
    unit              text          NOT NULL,                        -- the base unit of the item (as the stock opname)
    qty               numeric(20,6) NOT NULL CHECK (qty > 0),
    available         numeric(20,6),                                   -- the stock of the source when requested
    price             numeric(20,6),                                   -- at receipt
    value             numeric(18,2),
    PRIMARY KEY (transfer_id, line_no)
);

-- who approves the transfers (Monitoring Stock → Settings; superadmins always may)
CREATE TABLE IF NOT EXISTS integration_portal.stock_transfer_approver (
    user_id           uuid          PRIMARY KEY,
    added_by          text          NOT NULL,
    added_at          timestamptz   NOT NULL DEFAULT now()
);

-- the pages: the form for the PICs and the outlet accounts, Monitoring Stock for Cost Control
INSERT INTO integration_portal.role_page (role, page)
SELECT r, p FROM (VALUES ('user', 'form.stock-transfer'), ('outlet', 'form.stock-transfer'),
                         ('cost_control', 'cc.stock-list'), ('cost_control', 'cc.stock-transfer')) v(r, p)
ON CONFLICT DO NOTHING;
