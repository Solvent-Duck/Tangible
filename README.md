# Tangible

Operational due diligence for lenders. The agent investigates, the tools compute, the policy decides.
Full plan: [docs/build-plan.md](docs/build-plan.md). Work packages and status: [docs/implementation-plan.md](docs/implementation-plan.md).

## Quick start

```bash
make install   # uv sync
make db-up     # Postgres 16 in Docker
make migrate
make check     # ruff, mypy, pytest
```

Run the analyst workbench locally (dev login; production requires OIDC settings and refuses to start without them):

```bash
TANGIBLE_DEV_AUTH=1 uv run tangible-workbench   # http://127.0.0.1:8000
```

Seed a demo tenant (five synthetic borrowers dated to the last week, users ana/adm/len, open cases; prints the dev-login URL). Serve it with the `workbench-demo` launch config on port 8001:

```bash
make demo
```

## Layout

| Path | What |
| --- | --- |
| `src/tangible/db/` | Plain SQL migrations 0001–0008 (schema, RLS, immutability triggers), tenant sessions, repositories. Applied by `tangible-migrate`. |
| `src/tangible/ledger/` | Ledger row types and `known_as_of`, the single place the `observed_at` time rule lives. |
| `src/tangible/ingest/`, `src/tangible/datasets/` | Load data into the ledger (`tangible-ingest`); UCI Online Retail loader and a synthetic generator with planted problems. |
| `src/tangible/metrics/` | 23 versioned pure metrics (health and momentum), pinned as set `ms-2`. |
| `src/tangible/checks/` | 17 versioned integrity checks (reconciliation, completeness, tampering, documents), pinned as set `cs-4` (`books_vs_processor` v2 waits for settlement); they emit findings. |
| `src/tangible/policy/` | Policy loader (version + hash), typed schema, evaluator (band and limit) and review state. |
| `src/tangible/assessment/` | Baseline, freeze and replay of an assessment (`tangible-replay`). |
| `src/tangible/agent/` | Investigation loop: plan, analyst approval, then a step- and cost-budgeted tool run. Eight logged tools, an analysis sandbox, a number verifier (the agent is told which numbers did not verify and can record a correction that replaces the earlier finding) and versioned prompts (`plan@v3`, `investigate@v4`). `llm.py` is the only vendor-aware module (Bedrock Converse). |
| `src/tangible/documents/`, `src/tangible/web/` | Uploaded PDF statements (text, file traits, model reader) and allow-listed web lookups. Both are untrusted data. |
| `src/tangible/evidence/`, `src/tangible/secrets/` | Raw evidence store (local directory, or S3 with Object Lock and SSE-KMS) and connector tokens (environment or Secrets Manager). |
| `src/tangible/connectors/` | Rutter and Plaid adapters (`tangible-pull`) and sandbox seeding (`tangible-sandbox`). |
| `src/tangible/backtest/` | Runs a cohort as of each loan's decision date and reports loss by band, metric discrimination, finding lift, plants caught, replay matches and agent spread (`tangible-backtest`). |
| `src/tangible/workbench/` | Analyst workbench: FastAPI API and Jinja2/htmx UI with OIDC login (`tangible-workbench`). |
| `src/tangible/demo/` | Demo tenant seed (`tangible-demo seed`, `make demo`) and statement PDFs from a borrower's ledger (`tangible-demo statements`). |
| `policies/default.yaml` | Default lender policy 0.3.0 (starting-guess thresholds; agent holds block only once an analyst accepts them). Older versions are kept beside it (`default-0.2.2.yaml`) so replay can use the policy an assessment was frozen under. |

## Status

Everything in the layout above is built and covered by `make check`. Not done yet:

- **Backtest on real outcomes.** Needs a lender's historical file; the harness runs today on a synthetic cohort with planted labels.
- **Newer Claude models.** Live runs use Claude Sonnet 4.5 on Bedrock (with prompt caching, about $0.50–$1 per case). The Claude 5.x models are blocked by account quota pending an AWS Support case.
- **Production hosting.** AWS infrastructure-as-code, Akamai edge config and SOC 2 controls are outside the MVP, as the build plan says.

## Notes

- Ledger table `order` is named `orders` (reserved word). Every ledger table carries `source_ref` so a revised record is a new row, not an overwrite.
- App code must run as role `tangible_app` and `SET app.tenant_id` per connection; RLS is forced on every tenant table.
- Borrower data never goes in the repo (`data/` and `*.csv` are ignored). Two tests skip without the local UCI dataset.
