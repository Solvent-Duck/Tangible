-- A corrected agent finding replaces the earlier one instead of sitting beside it as a duplicate.
-- Findings stay append-only: the replacement is a new row that points at the row it supersedes
-- (the same idea as a revised ledger row sharing a source_ref). The older row is never touched;
-- a finding is "current" when no other finding replaces it.
--
-- finding is an existing tenant table: its RLS policy (tenant_isolation on tenant_id) already
-- covers new columns, so nothing is added there. finding has no update guard (review state lives
-- in underwriter_action), and none is added or removed here.
ALTER TABLE finding
    ADD COLUMN replaces_id uuid REFERENCES finding (id),
    ADD CONSTRAINT finding_not_self_replacing CHECK (replaces_id IS DISTINCT FROM id);

-- FK checks bypass RLS, so the plain reference above could point at another tenant's finding.
-- This composite reference (NULL replaces_id skips it) keeps a replacement in its tenant.
ALTER TABLE finding
    ADD CONSTRAINT finding_id_tenant_key UNIQUE (id, tenant_id),
    ADD CONSTRAINT finding_replaces_same_tenant
        FOREIGN KEY (replaces_id, tenant_id) REFERENCES finding (id, tenant_id);

-- A finding can be replaced at most once (a correction of a correction replaces the correction).
CREATE UNIQUE INDEX finding_replaces_once ON finding (replaces_id) WHERE replaces_id IS NOT NULL;
