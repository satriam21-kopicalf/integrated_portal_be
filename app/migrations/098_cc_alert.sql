-- 098: the notification bell of the portal (Cost Control, 2026-11-13; app/alerts.py). Alerts on goods receipts and
-- other ESB documents that look wrong — a quantity far from usual, a GR received far below its PO — and the daily
-- summaries (GRs still New after 2 days: outside the Stock List; corrected GRs not marked Finished; the GRs of
-- yesterday). Not one alert per GR received (~90 a day): only what needs a look. The scan runs in the background every
-- 30 minutes; `dedup` makes a scan harmless to repeat. Read by whoever opens Good Receipt or COGS & Usage, each user
-- with their own read marks.
CREATE TABLE IF NOT EXISTS integration_portal.cc_alert (
    id bigserial PRIMARY KEY,
    dedup text NOT NULL UNIQUE,
    kind text NOT NULL,
    severity text NOT NULL DEFAULT 'warn' CHECK (severity IN ('high', 'warn', 'info')),
    title text NOT NULL,
    body text,
    link text,
    branch_code text,
    doc_num text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_alert_created ON integration_portal.cc_alert (created_at DESC);

CREATE TABLE IF NOT EXISTS integration_portal.cc_alert_read (
    alert_id bigint NOT NULL REFERENCES integration_portal.cc_alert (id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    read_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (alert_id, user_id)
);
