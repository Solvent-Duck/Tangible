-- Uploaded documents (WP3B). document_extract holds fields a tool-less model call read from an
-- uploaded file. Like other ledger rows it is never overwritten (no UPDATE); a re-extraction is a
-- new row with the same source_ref. Values are untrusted until a check reconciles them.

CREATE TABLE document_extract (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                uuid NOT NULL REFERENCES tenant (id),
    borrower_id              uuid NOT NULL REFERENCES borrower (id),
    source_ref               text NOT NULL,
    evidence_pull_id         uuid NOT NULL REFERENCES evidence_pull (id),
    account_ref              text NOT NULL,
    period_start             date NOT NULL,
    period_end               date NOT NULL,
    opening_balance          numeric(14, 2),
    closing_balance          numeric(14, 2),
    lines                    jsonb NOT NULL,
    file_traits              jsonb NOT NULL DEFAULT '{}',
    extracted_by_model_call  uuid REFERENCES model_call (id),
    observed_at              timestamptz NOT NULL
);

CREATE INDEX document_extract_borrower ON document_extract (borrower_id, source_ref);

CREATE TRIGGER document_extract_no_update BEFORE UPDATE ON document_extract
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

ALTER TABLE document_extract ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_extract FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON document_extract
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
