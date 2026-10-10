-- Case and agent layer: a case_file is one investigation of a borrower as of a date; the agent's
-- plan, tool calls, model calls and drafted questions hang off it. All of these are append-only
-- evidence of what the agent did. case_file itself is a workflow record, so its status may change.
--
-- Review state is derived, never stored: a finding's review state is its latest underwriter_action
-- row. finding.status is never updated (that would break append-only review). Likewise plan and
-- question approval are underwriter_action rows (approve_plan / approve_question), not columns.
--
-- `case` is a reserved word, so the table is named case_file.

CREATE TABLE case_file (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant (id),
    borrower_id      uuid NOT NULL REFERENCES borrower (id),
    as_of            timestamptz NOT NULL,
    loan_amount      numeric(14, 2),
    budget_steps     int NOT NULL,
    budget_usd       numeric(10, 2) NOT NULL,
    assigned_analyst text,
    status           text NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'planning', 'investigating', 'review', 'signed', 'closed')),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_plan (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant (id),
    case_id    uuid NOT NULL REFERENCES case_file (id),
    version    int NOT NULL,
    body       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, version)
);

CREATE TABLE tool_call (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant (id),
    case_id       uuid NOT NULL REFERENCES case_file (id),
    tool          text NOT NULL,
    tool_version  int NOT NULL,
    args          jsonb NOT NULL,
    result        jsonb,
    result_sha256 text,
    error         text,
    started_at    timestamptz NOT NULL,
    duration_ms   int,
    CONSTRAINT tool_call_result_xor_error CHECK (num_nonnulls(result, error) = 1)
);

CREATE TABLE model_call (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant (id),
    case_id        uuid NOT NULL REFERENCES case_file (id),
    model_id       text NOT NULL,
    prompt_key     text NOT NULL,
    prompt_version text NOT NULL,
    prompt_sha256  text NOT NULL,
    input_tokens   int NOT NULL,
    output_tokens  int NOT NULL,
    cost_usd       numeric(10, 6) NOT NULL,
    at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE borrower_question (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenant (id),
    case_id              uuid NOT NULL REFERENCES case_file (id),
    body                 text NOT NULL,
    drafted_by_tool_call uuid REFERENCES tool_call (id),
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant (id),
    actor       text NOT NULL,
    action      text NOT NULL,
    object_kind text NOT NULL,
    object_id   uuid,
    detail      jsonb NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- existing tables

-- Rule findings from a plain baseline may predate any case, so case_id is nullable.
-- verified: NULL = not checked (rule findings leave it NULL).
ALTER TABLE finding
    ADD COLUMN case_id  uuid REFERENCES case_file (id),
    ADD COLUMN verified boolean;

ALTER TABLE underwriter_action
    ADD COLUMN case_id     uuid REFERENCES case_file (id),
    ADD COLUMN question_id uuid REFERENCES borrower_question (id),
    ADD COLUMN plan_id     uuid REFERENCES agent_plan (id);

-- 0001 left these constraints unnamed; Postgres auto-named them.
ALTER TABLE underwriter_action DROP CONSTRAINT IF EXISTS underwriter_action_action_check;
ALTER TABLE underwriter_action DROP CONSTRAINT IF EXISTS underwriter_action_check;

ALTER TABLE underwriter_action
    ADD CONSTRAINT underwriter_action_action_check
        CHECK (action IN ('dispute', 'override', 'request_more', 'accept', 'reject',
                          'approve_plan', 'approve_question', 'sign')),
    ADD CONSTRAINT underwriter_action_one_target
        CHECK (num_nonnulls(finding_id, assessment_id, case_id, question_id, plan_id) = 1);

-- ---------------------------------------------------------------- guards

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['agent_plan', 'tool_call', 'model_call', 'borrower_question',
                             'audit_log']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
                        FOR EACH ROW EXECUTE FUNCTION forbid_change()', t || '_append_only', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- tenancy (RLS)

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['case_file', 'agent_plan', 'tool_call', 'model_call',
                             'borrower_question', 'audit_log']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                          WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
