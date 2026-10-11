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

Run the workbench API locally (dev auth; otherwise it needs OIDC or Clerk settings and refuses to start without them):

```bash
TANGIBLE_DEV_AUTH=1 uv run tangible-workbench   # http://127.0.0.1:8000/api
```

Seed a demo tenant (five synthetic borrowers dated to the last week, users ana/adm/len, open cases; prints the web app's sign-in URL). Serve the API with the `workbench-demo` launch config on port 8001:

```bash
make demo
```

Run the web app (landing page, lender dashboard, invites, business onboarding, due diligence) against a dev-auth API. See [docs/onboarding.md](docs/onboarding.md) for the flow and Clerk setup, and [docs/deploy.md](docs/deploy.md) for hosting:

```bash
make web-install
TANGIBLE_API_URL=http://127.0.0.1:8000 make web-dev   # http://localhost:3000
```

## The agent team, on Strands Agents

The investigation runs on the [Strands Agents SDK](https://strandsagents.com) (`TANGIBLE_AGENT_ENGINE=strands`, the default; `classic` keeps the original hand-written loop as a fallback and a test oracle).

```
approve plan ─► Strands Graph "intangibles_search" (parallel)
                  presence_agent · loyalty_agent · reputation_agent   web tools only, small step caps
                         │ reports, quoted as untrusted data
                         ▼
                lead_investigator   ledger, checks, metrics, sandbox, documents, findings
every agent:  ModelClientModel  → agent/llm.py (Bedrock Converse, prompt caching, cost per call)
              TangibleGuardrails → Strands hooks enforcing the invariants below
                         ▼
ms-3 metrics (pure, versioned) ─► policy 0.4.0: knockout → bands → uplift (≤ 1 notch) → review cap
```

- **Custom model provider.** `strands_engine/model.py` is a Strands `Model` over our own `ModelClient`, so every model call still goes through `agent/llm.py`: one `model_call` row with its cost per step, Bedrock prompt caching, and scripted clients that drive Strands offline in tests.
- **Invariants as hooks.** `strands_engine/guardrails.py` is where "the agent investigates, tools compute, policy decides" is enforced: shared step and cost budgets (`BeforeModelCallEvent`), each agent limited to its own tools (`BeforeToolCallEvent`), every tool result quoted as untrusted data (`AfterToolCallEvent`), and wrap-up notices (`AfterToolsEvent`). Tools still run through `call_tool`, which validates the call and leaves an audit row.
- **A Graph of search agents.** Three agents look the business up in public sources in parallel. They match listings with a deterministic scorer they cannot override, record signals, and never score anything. A same-name listing in another city is refused, and a stranger's 5-star reputation never reaches the metrics.
- **Parity.** `tests/test_agent_loop.py` runs on both engines. `tests/test_strands_engine.py` checks that the model sees byte-identical prompts, tool schemas and results on both, and that tool calls, findings and cost come out the same.

**Demo (about 90 seconds).** Seed the tenant (`make demo`), serve the API with the `api-strands` launch config (port 8004) and the web app against it (`TANGIBLE_API_URL=http://127.0.0.1:8004 make web-dev`), and sign in as `ana`.
1. Open **Juniper & Rye Bakery LLC**: propose the plan, approve it, then run the investigation. The status panel's live feed shows the presence, loyalty and reputation agents working in parallel, then the lead investigator.
2. When it finishes, **Public signals** lists the matched Google, Yelp and domain records with their match scores, and **Agent run** shows the Strands team with each agent's steps and tool calls.
3. Sign it. The assessment reads "uplift C→B via loyalty: repeat_customer_share … loyalty_mention_share …". Reputation was computed but shows as disabled by policy, because ratings have no history to backtest.
4. Open **Northgate Phone Repair LLC** and run it. The same-name Spokane shop scores 0.40 and `record_signal` refuses it, even though its review text tries to talk the agent into claiming it.

Env for a live run: `TANGIBLE_MODEL_BACKEND=bedrock TANGIBLE_SIGNAL_PROVIDER=fixtures` plus the Bedrock settings in the Makefile `demo` comment. One case costs about $0.80 on Claude Sonnet 4.5.

## Systemic exposure (tariffs)

Tangible does not forecast tariffs. It loads enacted or announced actions, dated by publication (`tangible-macro load`; the demo set is fictional), and reads each borrower's stated import mix. The ms-4 metrics then compute the margin and cash flow left after the new duty, assuming none of it is passed on. Policy 0.5.0 may only **reduce the limit**, never the band, for example "stressed limit 114,182.91→85,637.18 (x0.75): stressed_flow_margin −0.018 ≤ 0". The cases list shows how much of the book each action touches. Plan and v1 notes: [docs/systemic-risk-plan.md](docs/systemic-risk-plan.md).

## Layout

| Path | What |
| --- | --- |
| `src/tangible/db/` | Plain SQL migrations 0001–0011 (schema, RLS, immutability triggers), tenant sessions, repositories. Applied by `tangible-migrate`. |
| `src/tangible/ledger/` | Ledger row types and `known_as_of`, the single place the `observed_at` time rule lives. |
| `src/tangible/ingest/`, `src/tangible/datasets/` | Load data into the ledger (`tangible-ingest`); UCI Online Retail loader and a synthetic generator with planted problems. |
| `src/tangible/metrics/` | 23 versioned pure metrics (health and momentum), pinned as set `ms-2`. |
| `src/tangible/checks/` | 17 versioned integrity checks (reconciliation, completeness, tampering, documents), pinned as set `cs-4` (`books_vs_processor` v2 waits for settlement); they emit findings. |
| `src/tangible/policy/` | Policy loader (version + hash), typed schema, evaluator (band and limit) and review state. |
| `src/tangible/assessment/` | Baseline, freeze and replay of an assessment (`tangible-replay`). |
| `src/tangible/agent/strands_engine/` | The Strands Agents engine: model provider, guardrail hooks, the search-team Graph. |
| `src/tangible/agent/` | Investigation loop: plan, analyst approval, then a step- and cost-budgeted tool run. Eight logged tools, an analysis sandbox, a number verifier (the agent is told which numbers did not verify and can record a correction that replaces the earlier finding) and versioned prompts (`plan@v3`, `investigate@v4`). `llm.py` is the only vendor-aware module (Bedrock Converse). |
| `src/tangible/documents/`, `src/tangible/web/` | Uploaded PDF statements (text, file traits, model reader) and allow-listed web lookups. Both are untrusted data. |
| `src/tangible/evidence/`, `src/tangible/secrets/` | Raw evidence store (local directory, or S3 with Object Lock and SSE-KMS) and connector tokens (environment or Secrets Manager). |
| `src/tangible/connectors/` | Rutter and Plaid adapters (`tangible-pull`) and sandbox seeding (`tangible-sandbox`). |
| `src/tangible/backtest/` | Runs a cohort as of each loan's decision date and reports loss by band, metric discrimination, finding lift, plants caught, replay matches and agent spread (`tangible-backtest`). |
| `src/tangible/workbench/` | Workbench: the FastAPI JSON API (`tangible-workbench`) with dev, OIDC (bearer tokens) or Clerk auth, and `tangible-admin` for staff users. |
| `src/tangible/onboarding/`, `src/tangible/notify/` | Lender invites, the versioned request catalog, the business's onboarding, and the email outbox. |
| `web/` | The web app (Next.js): landing page, lender dashboard and invite composer, business onboarding, due-diligence workspace. |
| `src/tangible/demo/` | Demo tenant seed (`tangible-demo seed`, `make demo`) and statement PDFs from a borrower's ledger (`tangible-demo statements`). |
| `policies/default.yaml` | Default lender policy 0.5.0 (starting-guess thresholds; agent holds block only once an analyst accepts them; 0.4.0 added the intangibles uplift, 0.5.0 the stressed limit). Older versions are kept beside it (`default-0.2.2.yaml` … `default-0.4.0.yaml`) so replay can use the policy an assessment was frozen under. |

## Status

Everything in the layout above is built and covered by `make check`. Not done yet:

- **Backtest on real outcomes.** Needs a lender's historical file; the harness runs today on a synthetic cohort with planted labels.
- **Newer Claude models.** Live runs use Claude Sonnet 4.5 on Bedrock (with prompt caching, about $0.50–$1 per case). The Claude 5.x models are blocked by account quota pending an AWS Support case.
- **Production hosting.** AWS infrastructure-as-code, Akamai edge config and SOC 2 controls are outside the MVP, as the build plan says.

## Notes

- Ledger table `order` is named `orders` (reserved word). Every ledger table carries `source_ref` so a revised record is a new row, not an overwrite.
- App code must run as role `tangible_app` and `SET app.tenant_id` per connection; RLS is forced on every tenant table.
- Borrower data never goes in the repo (`data/` and `*.csv` are ignored). Two tests skip without the local UCI dataset.
