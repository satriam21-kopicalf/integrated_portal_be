-- 038: Damaged Equipment approvals (2026-10-29, user request): Review → Accept goes through the approval steps set by
-- management per kind (Maintenance, Procurement), as Form Fund (integration_portal.approval_step, form 'de'). A request
-- in Review waits at approval_step 1, 2 …; the last step accepts it. A kind without steps: any monitoring account
-- accepts, as before.
ALTER TABLE integration_portal.de_request ADD COLUMN IF NOT EXISTS approval_step integer NOT NULL DEFAULT 1;
