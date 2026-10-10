-- Workbench: app users (who may sign in, and as what) and re-based cases.
-- Authorization data is tenant data, so app_user carries RLS on tenant_id like every other table.
-- Everything else the workbench records (views, exports, overrides, signatures) goes to audit_log
-- and underwriter_action, which already exist.

CREATE TABLE app_user (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenant (id),
    subject      text NOT NULL,
    email        text,
    display_name text,
    role         text NOT NULL CHECK (role IN ('analyst', 'lender_underwriter', 'credit_admin')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, subject)
);

-- A re-based case points at the case it supersedes.
ALTER TABLE case_file ADD COLUMN previous_case_id uuid REFERENCES case_file (id);

-- ---------------------------------------------------------------- tenancy (RLS)

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_user
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tangible_app;
