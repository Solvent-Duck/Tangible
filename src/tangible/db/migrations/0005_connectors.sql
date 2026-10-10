-- Connector accounts (WP4C): which external account a connection pulls from, and sync state.
-- The access token is NEVER stored here; `secret_ref` points into the secret store
-- (Secrets Manager in production, env in dev).

CREATE TABLE connector_account (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenant (id),
    borrower_id         uuid NOT NULL REFERENCES borrower (id),
    connection_id       uuid NOT NULL REFERENCES connection (id),
    provider            text NOT NULL CHECK (provider IN ('rutter', 'plaid')),
    secret_ref          text NOT NULL,
    external_account_id text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX connector_account_connection ON connector_account (connection_id);

-- Sync state, not evidence: mutable, one row per (account, stream).
CREATE TABLE connector_cursor (
    tenant_id            uuid NOT NULL REFERENCES tenant (id),
    connector_account_id uuid NOT NULL REFERENCES connector_account (id),
    stream               text NOT NULL,
    cursor               text NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connector_account_id, stream)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['connector_account', 'connector_cursor']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                          WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
