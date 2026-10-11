-- Borrower onboarding: a lender invites a business by email, the business creates an account,
-- tells us who it is and connects its systems.
-- (Numbered after 0009_public_signals and 0010_macro_signals; it touches different tables.)
--
-- invite: immutable. The raw token is never stored, only its SHA-256. A re-send is a new invite.
-- invite_event: append-only; an invite's status is derived from its events, never stored.
-- borrower_profile: the borrower's own account of who it is. Append-only: a correction is a new
--   row, and the latest one observed by a date is what was known then. Borrower-entered text is
--   untrusted data. Each save also appends an `application` row (source_ref onboarding:<borrower>)
--   so the investigation's identity matching and the tariff stress read what the business stated.
-- outbound_email: the non-production outbox. Every email the app "sends" is recorded here.

-- ---------------------------------------------------------------- borrower users

ALTER TABLE app_user DROP CONSTRAINT app_user_role_check;
ALTER TABLE app_user
    ADD CONSTRAINT app_user_role_check
        CHECK (role IN ('analyst', 'lender_underwriter', 'credit_admin', 'borrower')),
    ADD COLUMN borrower_id uuid REFERENCES borrower (id),
    ADD CONSTRAINT app_user_borrower_scope CHECK ((role = 'borrower') = (borrower_id IS NOT NULL));

-- ---------------------------------------------------------------- invites

CREATE TABLE invite (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant (id),
    borrower_id     uuid NOT NULL REFERENCES borrower (id),
    email           text NOT NULL,
    contact_name    text,
    business_name   text NOT NULL,
    loan_amount     numeric(14, 2),
    message         text,
    requests        jsonb NOT NULL,
    catalog_version text NOT NULL,
    token_hash      bytea NOT NULL UNIQUE,
    replaces_id     uuid REFERENCES invite (id),
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL
);

CREATE INDEX invite_borrower ON invite (borrower_id, created_at);

CREATE TABLE invite_event (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant (id),
    invite_id uuid NOT NULL REFERENCES invite (id),
    kind      text NOT NULL CHECK (kind IN ('sent', 'opened', 'accepted', 'profile_saved',
                                            'source_connected', 'document_added', 'link_added',
                                            'answer_added',
                                            'submitted', 'resent', 'revoked', 'case_opened')),
    item_id   text,
    detail    jsonb NOT NULL DEFAULT '{}',
    actor     text,
    at        timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX invite_event_invite ON invite_event (invite_id, at);

CREATE TABLE borrower_profile (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant (id),
    borrower_id        uuid NOT NULL REFERENCES borrower (id),
    legal_name         text,
    dba                text,
    website            text,
    phone              text,
    address            text,
    state              text,
    naics              text,
    year_founded       int,
    stated_revenue_90d numeric(14, 2),
    import_mix         jsonb,  -- [{hs_chapter, country, share_of_cogs}]; [] = imports nothing
    observed_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    submitted_by       text NOT NULL
);

CREATE INDEX borrower_profile_borrower ON borrower_profile (borrower_id, observed_at);

CREATE TABLE outbound_email (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenant (id),
    invite_id    uuid REFERENCES invite (id),
    to_addr      text NOT NULL,
    subject      text NOT NULL,
    body_text    text NOT NULL,
    body_html    text NOT NULL,
    transport    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    error        text
);

CREATE INDEX outbound_email_created ON outbound_email (created_at);

-- ---------------------------------------------------------------- guards

CREATE TRIGGER invite_append_only BEFORE UPDATE OR DELETE ON invite
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER invite_event_append_only BEFORE UPDATE OR DELETE ON invite_event
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER borrower_profile_append_only BEFORE UPDATE OR DELETE ON borrower_profile
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- ---------------------------------------------------------------- cross-tenant lookups
-- The only two questions asked before a tenant is known. SECURITY DEFINER functions run as the
-- migrating (owner) role, which bypasses RLS like admin_connect does; each returns the minimum
-- needed to open a tenant_session, never tenant data.

CREATE FUNCTION invite_by_token(p_hash bytea)
RETURNS TABLE (invite_id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT i.id, i.tenant_id FROM invite i WHERE i.token_hash = p_hash
$$;

CREATE FUNCTION memberships_for_subject(p_subject text)
RETURNS TABLE (tenant_id uuid, tenant_name text, role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT u.tenant_id, t.name, u.role
    FROM app_user u JOIN tenant t ON t.id = u.tenant_id
    WHERE u.subject = p_subject
    ORDER BY t.created_at DESC, t.id
$$;

REVOKE ALL ON FUNCTION invite_by_token(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION memberships_for_subject(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION invite_by_token(bytea) TO tangible_app;
GRANT EXECUTE ON FUNCTION memberships_for_subject(text) TO tangible_app;

-- ---------------------------------------------------------------- tenancy (RLS)

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['invite', 'invite_event', 'borrower_profile', 'outbound_email']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant_isolation ON %I
                          USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                          WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
END $$;

-- Only the new tables: a blanket GRANT ON ALL TABLES would undo 0010's read-only macro_signal.
GRANT SELECT, INSERT, UPDATE, DELETE ON invite, invite_event, borrower_profile, outbound_email
    TO tangible_app;
