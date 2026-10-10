-- Four layers: raw evidence, common ledger, derived, assessment.
-- Time rule: every ledger row has occurred_at (business time) and observed_at (when we learned it).
-- Revisions are new rows sharing a source_ref; old rows are never overwritten.
-- evidence_ref = (evidence_pull_id, evidence_locator): the pull plus row/byte offset in the raw file.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Roles are cluster-wide, so tolerate it already existing (e.g. a second database on the cluster).
DO $$
BEGIN
    CREATE ROLE tangible_app NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE tenant (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE borrower (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant (id),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- raw evidence

CREATE TABLE connection (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant (id),
    borrower_id     uuid NOT NULL REFERENCES borrower (id),
    source          text NOT NULL,
    granted_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    scopes          text[] NOT NULL DEFAULT '{}',
    last_success_at timestamptz,
    last_error      text
);

CREATE TABLE evidence_pull (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant (id),
    borrower_id   uuid NOT NULL REFERENCES borrower (id),
    source        text NOT NULL,
    connection_id uuid REFERENCES connection (id),
    fetched_at    timestamptz NOT NULL,
    s3_key        text NOT NULL,
    sha256        text NOT NULL,
    byte_size     bigint NOT NULL,
    status        text NOT NULL
);

-- ---------------------------------------------------------------- common ledger
-- Every ledger table also carries tenant_id, borrower_id, source_ref (revision identity),
-- observed_at, evidence_pull_id and evidence_locator.

CREATE TABLE orders (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    channel           text,
    gross             numeric(14, 2) NOT NULL,
    discounts         numeric(14, 2) NOT NULL DEFAULT 0,
    tax               numeric(14, 2) NOT NULL DEFAULT 0,
    currency          char(3) NOT NULL,
    customer_ref      text,
    occurred_at       timestamptz NOT NULL,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE refund (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    order_ref         text NOT NULL,
    amount            numeric(14, 2) NOT NULL,
    reason            text,
    occurred_at       timestamptz NOT NULL,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE payout (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    processor         text NOT NULL,
    amount            numeric(14, 2) NOT NULL,
    fees              numeric(14, 2) NOT NULL DEFAULT 0,
    expected_on       date,
    settled_on        date,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE bank_txn (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    account_ref       text NOT NULL,
    amount            numeric(14, 2) NOT NULL,
    direction         text NOT NULL CHECK (direction IN ('in', 'out')),
    counterparty      text,
    description       text,
    posted_on         date NOT NULL,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE invoice (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    kind              text NOT NULL CHECK (kind IN ('payable', 'receivable')),
    counterparty      text,
    amount            numeric(14, 2) NOT NULL,
    issued_on         date NOT NULL,
    due_on            date,
    paid_on           date,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE inventory_snapshot (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    sku               text NOT NULL,
    units_on_hand     numeric(14, 2) NOT NULL,
    unit_cost         numeric(14, 4),
    as_of             date NOT NULL,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE account_balance (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    account_ref       text NOT NULL,
    balance           numeric(14, 2) NOT NULL,
    as_of             date NOT NULL,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

CREATE TABLE support_ticket (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant (id),
    borrower_id       uuid NOT NULL REFERENCES borrower (id),
    source_ref        text NOT NULL,
    opened_at         timestamptz NOT NULL,
    closed_at         timestamptz,
    category          text,
    severity          text,
    observed_at       timestamptz NOT NULL,
    evidence_pull_id  uuid NOT NULL REFERENCES evidence_pull (id),
    evidence_locator  text NOT NULL
);

-- ---------------------------------------------------------------- derived

CREATE TABLE metric_value (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant (id),
    borrower_id    uuid NOT NULL REFERENCES borrower (id),
    metric_key     text NOT NULL,
    metric_version int NOT NULL,
    as_of          timestamptz NOT NULL,
    value          numeric,
    input_hash     text NOT NULL,
    UNIQUE (borrower_id, metric_key, metric_version, as_of, input_hash)
);

CREATE TABLE finding (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant (id),
    borrower_id    uuid NOT NULL REFERENCES borrower (id),
    check_key      text NOT NULL,
    check_version  int NOT NULL,
    origin         text NOT NULL DEFAULT 'rule' CHECK (origin IN ('rule', 'model')),
    severity       text NOT NULL CHECK (severity IN ('info', 'review', 'hold')),
    direction      text CHECK (direction IN ('raises_risk', 'lowers_risk')),
    pillar         text NOT NULL CHECK (pillar IN ('health', 'momentum', 'integrity')),
    summary        text NOT NULL,
    evidence_refs  jsonb NOT NULL DEFAULT '[]',
    tool_call_ids  text[] NOT NULL DEFAULT '{}',
    status         text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'explained', 'disputed', 'confirmed')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- agent findings must cite the tool calls behind them
    CONSTRAINT model_findings_cite_tools CHECK (origin <> 'model' OR cardinality(tool_call_ids) > 0)
);

-- ---------------------------------------------------------------- assessment

CREATE TABLE assessment (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant (id),
    borrower_id        uuid NOT NULL REFERENCES borrower (id),
    as_of              timestamptz NOT NULL,
    evidence_set_hash  text NOT NULL,
    metric_set_version text NOT NULL,
    policy_version     text NOT NULL,
    policy_hash        text NOT NULL,
    model_id           text,
    prompt_versions    jsonb NOT NULL DEFAULT '{}',
    decision_band      text CHECK (decision_band IN ('A', 'B', 'C', 'D')),  -- NULL = no decision (hold)
    suggested_limit    numeric(14, 2),
    rules_fired        jsonb NOT NULL DEFAULT '[]',
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text NOT NULL
);

CREATE TABLE assessment_item (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant (id),
    assessment_id uuid NOT NULL REFERENCES assessment (id),
    kind          text NOT NULL CHECK (kind IN ('metric', 'finding')),
    ref_id        uuid NOT NULL
);

-- Overrides sit beside the assessment, never inside it.
CREATE TABLE underwriter_action (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant (id),
    finding_id    uuid REFERENCES finding (id),
    assessment_id uuid REFERENCES assessment (id),
    user_id       text NOT NULL,
    action        text NOT NULL CHECK (action IN ('dispute', 'override', 'request_more')),
    reason        text NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(finding_id, assessment_id) = 1)
);

-- ---------------------------------------------------------------- guards

CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: table is immutable', TG_OP, TG_TABLE_NAME;
END $$;

-- Ledger rows are never overwritten (deletion requests may still DELETE).
-- Assessments and underwriter actions are fully append-only.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['orders', 'refund', 'payout', 'bank_txn', 'invoice',
                             'inventory_snapshot', 'account_balance', 'support_ticket',
                             'evidence_pull']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION forbid_change()', t || '_no_update', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['assessment', 'assessment_item', 'underwriter_action']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
                        FOR EACH ROW EXECUTE FUNCTION forbid_change()', t || '_append_only', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- tenancy (RLS)
-- The app sets `SET app.tenant_id = '<uuid>'` per connection/transaction and runs as tangible_app.
-- FORCE makes the table owner subject to the policy too; only superusers bypass it.

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['borrower', 'connection', 'evidence_pull', 'orders', 'refund',
                             'payout', 'bank_txn', 'invoice', 'inventory_snapshot',
                             'account_balance', 'support_ticket', 'metric_value', 'finding',
                             'assessment', 'assessment_item', 'underwriter_action']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                          WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant
    USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
