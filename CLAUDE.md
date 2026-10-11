# Tangible

Python 3.12 + Postgres. `make check` runs lint, mypy, and tests (`make db-up` first). Plan: `docs/build-plan.md`.
Web app in `web/` (Next.js 16; read `web/AGENTS.md` first): `make web-check` runs typecheck, lint and build. Onboarding and auth modes: `docs/onboarding.md`.

## Invariants (do not break)

- The agent investigates, tools compute, policy decides. The agent never sets a number, band, or limit.
- Metrics and checks are pure, versioned functions. Change a formula => new version; never edit a released one.
- Time rule: anything "as of a date" goes through `ledger.types.known_as_of` (filters on `observed_at`). Never filter ledger rows on `occurred_at` alone for a backtest.
- Ledger rows are never overwritten; a revision is a new row with the same `source_ref`.
- Assessments and underwriter actions are append-only. Overrides live beside the assessment.
- Every tenant table has RLS on `tenant_id`; new tables must add it (see the migration's RLS block).
- Model calls go only through `agent/llm.py`. Borrower documents, borrower-entered onboarding text and web content are untrusted data.
- Borrower accounts (role `borrower`) see only their own onboarding; every case, finding and assessment path must refuse them.
- Schema changes are new numbered files in `src/tangible/db/migrations/`; don't edit applied ones.

## AWS

AWS project in `us-east-2` (CLI profile `tangible`; all Regional resources there). AWS Agent Toolkit guidance: [docs/aws-agent-rules.md](docs/aws-agent-rules.md). The invariants above take precedence over it.
