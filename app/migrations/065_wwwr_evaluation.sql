-- 065 (2026-11-03): WW-WR evaluation and follow-up. Before the lead sends an audit the team writes the general
-- evaluation, the action plans (what to do, who of the team handles it, by when) and the day of the revisit. A sent
-- audit stays "follow-up" in the monitoring until the team marks every action plan done and the lead closes the
-- revisit; then it is complete. share_token: the read-only link (and print / PDF) given to the PIC of the outlet.
ALTER TABLE integration_portal.wwwr_audit
    ADD COLUMN IF NOT EXISTS summary       text,
    ADD COLUMN IF NOT EXISTS revisit_on    date,
    ADD COLUMN IF NOT EXISTS revisit_note  text,
    ADD COLUMN IF NOT EXISTS completed_at  timestamptz,
    ADD COLUMN IF NOT EXISTS completed_by  uuid,
    ADD COLUMN IF NOT EXISTS share_token   text NOT NULL DEFAULT replace(gen_random_uuid()::text, '-', '');
CREATE UNIQUE INDEX IF NOT EXISTS wwwr_audit_share_token ON integration_portal.wwwr_audit (share_token);

CREATE TABLE IF NOT EXISTS integration_portal.wwwr_action (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_id    uuid        NOT NULL REFERENCES integration_portal.wwwr_audit (id) ON DELETE CASCADE,
    title       text        NOT NULL,
    detail      text,
    assignee_id uuid,
    due_on      date,
    status      text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    done_at     timestamptz,
    done_by     uuid,
    done_note   text,
    sort        integer     NOT NULL DEFAULT 0,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wwwr_action_audit ON integration_portal.wwwr_action (audit_id, sort);
CREATE INDEX IF NOT EXISTS wwwr_action_assignee ON integration_portal.wwwr_action (assignee_id) WHERE status = 'open';
