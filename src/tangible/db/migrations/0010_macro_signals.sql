-- Systemic risk v1 (docs/systemic-risk-plan.md, hackathon slice): dated public facts about the
-- world (tariff actions) and the borrower's stated import mix.
--
-- macro_signal is GLOBAL, not tenant data: a tariff applies to every borrower alike, so it has no
-- tenant_id and no RLS. It is written only by the trusted loader (`tangible-macro load`, as the
-- owning role); the app role may only read it. Append-only: a revised notice is a new row with the
-- same (source, source_ref). observed_at is when the fact became public (the notice's
-- publication date), never its effective date, so a backtest cannot see a tariff announced later.
--
-- application.import_mix: the applicant's stated imports as a JSON list of
-- {"hs_chapter": "94", "country": "CN", "share_of_cogs": 0.6}. Self-reported (soft) in v1.

CREATE TABLE macro_signal (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source       text NOT NULL,
    source_ref   text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('tariff_action')),
    payload      jsonb NOT NULL,
    url          text,
    sha256       text NOT NULL,
    occurred_at  timestamptz,
    observed_at  timestamptz NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_ref, sha256)
);

CREATE INDEX macro_signal_kind ON macro_signal (kind, observed_at);

CREATE TRIGGER macro_signal_append_only BEFORE UPDATE OR DELETE ON macro_signal
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- Global rows: tenants read them, only the loader (owning role) writes them.
REVOKE ALL ON macro_signal FROM tangible_app;
GRANT SELECT ON macro_signal TO tangible_app;

ALTER TABLE application ADD COLUMN import_mix jsonb;
