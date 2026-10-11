-- Intangibles v1 (docs/intangibles-plan.md): the loan application as a ledger row, public-web
-- signals and the entity matches that gate them.
--
-- application: the borrower's stated identity (v2 row shape: legal name, DBA, address, phone,
-- website, state, NAICS, requested amount). Like every ledger table it is never overwritten; a
-- corrected application is a new row with the same source_ref.
--
-- entity_match: written only by the match_entity tool. The score is computed by a deterministic,
-- versioned matcher; the agent cannot set it. Analyst confirmation is an underwriter_action
-- (confirm_match / reject_match) beside the match, never a change to it.
--
-- public_signal: written only by record_signal, from a matched source result. Append-only: a
-- re-pull is a new row with the same (source, source_ref); `known_as_of` picks the latest one
-- observed by the as-of date.

CREATE TABLE application (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenant (id),
    borrower_id         uuid NOT NULL REFERENCES borrower (id),
    source_ref          text NOT NULL,
    stated_revenue_90d  numeric(14, 2),
    own_account_names   text[] NOT NULL DEFAULT '{}',
    owner_names         text[] NOT NULL DEFAULT '{}',
    legal_name          text,
    dba                 text,
    address             text,
    phone               text,
    website             text,
    state               text,
    naics               text,
    requested_amount    numeric(14, 2),
    observed_at         timestamptz NOT NULL,
    evidence_pull_id    uuid REFERENCES evidence_pull (id),
    evidence_locator    text
);

CREATE INDEX application_borrower ON application (borrower_id, source_ref);

CREATE TABLE entity_match (
    id               uuid PRIMARY KEY,
    tenant_id        uuid NOT NULL REFERENCES tenant (id),
    case_id          uuid NOT NULL REFERENCES case_file (id),
    source           text NOT NULL,
    source_ref       text NOT NULL,
    source_call_id   uuid NOT NULL REFERENCES tool_call (id),
    candidate        jsonb NOT NULL,
    components       jsonb NOT NULL DEFAULT '{}',
    score            numeric(5, 4) NOT NULL CHECK (score >= 0 AND score <= 1),
    matcher_version  int NOT NULL,
    tool_call_id     uuid NOT NULL REFERENCES tool_call (id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT entity_match_id_tenant_key UNIQUE (id, tenant_id)
);

CREATE INDEX entity_match_case ON entity_match (case_id);

CREATE TABLE public_signal (
    id                uuid PRIMARY KEY,
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    case_id           uuid REFERENCES case_file (id),
    source            text NOT NULL,
    source_ref        text NOT NULL,
    kind              text NOT NULL,
    payload           jsonb NOT NULL,
    url               text,
    sha256            text,
    evidence_pull_id  uuid REFERENCES evidence_pull (id),
    entity_match_id   uuid NOT NULL,
    tool_call_id      uuid REFERENCES tool_call (id),
    occurred_at       timestamptz,
    observed_at       timestamptz NOT NULL,
    -- FK checks bypass RLS: the composite reference keeps a signal's match in its tenant.
    CONSTRAINT public_signal_match_same_tenant
        FOREIGN KEY (entity_match_id, tenant_id) REFERENCES entity_match (id, tenant_id)
);

CREATE INDEX public_signal_borrower ON public_signal (borrower_id, source, source_ref);

-- ---------------------------------------------------------------- analyst confirmation

ALTER TABLE underwriter_action ADD COLUMN entity_match_id uuid;
ALTER TABLE underwriter_action
    ADD CONSTRAINT underwriter_action_match_same_tenant
        FOREIGN KEY (entity_match_id, tenant_id) REFERENCES entity_match (id, tenant_id);

ALTER TABLE underwriter_action DROP CONSTRAINT underwriter_action_action_check;
ALTER TABLE underwriter_action DROP CONSTRAINT underwriter_action_one_target;
ALTER TABLE underwriter_action
    ADD CONSTRAINT underwriter_action_action_check
        CHECK (action IN ('dispute', 'override', 'request_more', 'accept', 'reject',
                          'approve_plan', 'approve_question', 'sign',
                          'confirm_match', 'reject_match')),
    ADD CONSTRAINT underwriter_action_one_target
        CHECK (num_nonnulls(finding_id, assessment_id, case_id, question_id, plan_id,
                            entity_match_id) = 1),
    ADD CONSTRAINT underwriter_action_match_actions
        CHECK ((entity_match_id IS NULL) = (action NOT IN ('confirm_match', 'reject_match')));

-- ---------------------------------------------------------------- guards

CREATE TRIGGER application_no_update BEFORE UPDATE ON application
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER public_signal_no_update BEFORE UPDATE ON public_signal
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER entity_match_append_only BEFORE UPDATE OR DELETE ON entity_match
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- ---------------------------------------------------------------- tenancy (RLS)

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['application', 'entity_match', 'public_signal']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                          WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
