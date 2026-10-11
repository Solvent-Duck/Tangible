# Hack Midwest 2026: judging guide

Tangible was built at Hack Midwest 2026 (Kansas City, Oct 10–11). The first commit is
`Initial scaffold` at 2026-10-10 13:37 CT; `git log --reverse` shows the whole build.

This page maps each judging question to the code that answers it, so a judge (or a judge's agent)
can check every claim. Paths are relative to the repo root.

- **AWS Challenge: Best Use of Strands Agents SDK.** Tangible's investigation runs on Strands: a
  custom `Model` provider, a `HookProvider` that enforces the product's safety rules, and a
  `GraphBuilder` team of parallel agents.
- **Flex Challenge: Best FinTech App.** Tangible is a small-business lending product: due diligence
  on a borrower's sales, payouts and bank deposits, ending in a credit band and a dollar limit
  from a versioned lender policy.

## In one minute

A lender has to decide whether to lend to a small business. Today an analyst spends days pulling
its bank, processor and bookkeeping data, reconciling them, and checking whether the business is
real and well regarded. Tangible does that work with a team of AI agents and stays safe enough
for credit. **The agent investigates, the tools compute, the policy decides.** The agents choose
what to look at and write findings. Every number comes from a deterministic, versioned tool, and
the agents never set a band or a limit.

- Live web app: https://web-production-e3b1a.up.railway.app (workspace behind sign-in for demo accounts)
- Borrower onboarding, no sign-in: https://web-production-e3b1a.up.railway.app/demo/onboarding
- All data is synthetic.

## AWS Challenge: "Does the project integrate Strands Agents SDK?"

Yes. Strands is the default engine for every investigation (`strands-agents==1.57.1` in
`pyproject.toml`; `TANGIBLE_AGENT_ENGINE=strands` is the default in
`src/tangible/agent/loop.py:323`).

```
analyst approves the plan
        │
        ▼
Strands Graph "intangibles_search"          (GraphBuilder, 3 entry points, no edges = parallel)
  presence_agent · loyalty_agent · reputation_agent
  web tools only · 6 steps each · shared budget
        │ closing reports, quoted as untrusted data
        ▼
Strands Agent "lead_investigator"            (ledger, checks, metrics, sandbox, documents, findings)
        │
every agent:  model  = ModelClientModel   → agent/llm.py → Amazon Bedrock Converse (Claude, prompt caching)
              hooks  = TangibleGuardrails  → budgets, tool scope, untrusted-data quoting, wrap-up
        ▼
pure versioned metrics (ms-4) ─► lender policy 0.7.0 ─► band + limit (+ tariff stress)
```

| Strands API | Where | What it does for us |
| --- | --- | --- |
| `strands.Agent` | `src/tangible/agent/strands_engine/__init__.py:18` (`build_agent`) | One factory wires every agent: our model, our tools, our hooks, `SequentialToolExecutor` (tools share one DB transaction) and `NullConversationManager` (the full history is the audit trail, never trimmed). |
| `strands.models.Model` | `src/tangible/agent/strands_engine/model.py:130` (`ModelClientModel`) | A custom model provider over our own client, so each Strands step goes through `agent/llm.py` (Bedrock Converse, prompt caching), is logged as a `model_call` row with its dollar cost, and can be replaced by a scripted client in tests. |
| `strands.hooks.HookProvider` | `src/tangible/agent/strands_engine/guardrails.py:99` (`TangibleGuardrails`) | The product's safety rules, enforced in code rather than asked for in the prompt. |
| `BeforeModelCallEvent` | `guardrails.py:116` | Shared step and dollar budgets across all agents in a run; `event.cancel` ends the turn cleanly. Each search agent also has its own step cap. |
| `BeforeToolCallEvent` | `guardrails.py:134` | Each agent can call only its own tools; `cancel_tool` blocks the rest. An invented tool name is routed to our validator so it leaves an audit row. |
| `AfterToolCallEvent` | `guardrails.py:144` | Every tool result, including Strands' own error results, is rewritten as a quoted `<tool_output untrusted="true">` block. Borrower documents and web pages can't inject instructions. |
| `AfterToolsEvent` | `guardrails.py:156` | Adds a trusted wrap-up notice when the budget is nearly spent or tools keep failing. |
| `strands.tools.tools.PythonAgentTool` | `guardrails.py:66` (`tangible_tools`) | Our tools, with schemas narrowed to the case, exposed to Strands. Every call still runs through `call_tool`, which validates it, runs it in a savepoint and writes a `tool_call` row. |
| `strands.multiagent.GraphBuilder` | `src/tangible/agent/strands_engine/team.py:51` (`run_search_team`) | Three search agents as a Graph with three entry points and no edges, so they run in parallel, with `set_max_node_executions` and `set_execution_timeout` as bounds. Their reports reach the lead as `<agent_report untrusted="true">`. |

### "Is the use of Strands unique, innovative or creative?"

1. **Strands as a regulated-credit runtime.** Lending needs an audit trail and needs the model kept
   away from the decision. The hooks enforce both: budgets, tool scope and untrusted-data quoting
   run on every call, and the agent has no tool that sets a number, band or limit.
2. **A custom model provider, not the stock Bedrock one.** `ModelClientModel` keeps one vendor-aware
   module (`agent/llm.py`), per-step cost accounting and prompt caching, and lets the whole Strands
   loop run offline against scripted model responses.
3. **Parity with a hand-written loop.** The original loop (`TANGIBLE_AGENT_ENGINE=classic`) stays as
   a test oracle. `tests/test_strands_engine.py::test_strands_and_classic_engines_are_equivalent`
   checks that both engines send the model byte-identical prompts, tool schemas and tool results,
   and produce the same tool calls, findings and cost.
4. **A parallel agent team that cannot be talked into a wrong answer.** The search agents match
   public listings with a deterministic scorer they can't override. In the demo a same-name
   phone-repair shop in another city scores 0.40, and `record_signal` refuses it even though its
   review text tells the agent to claim it
   (`test_search_team_records_true_listings_and_the_decoy_is_refused`).

### AWS services

| Service | Use | State |
| --- | --- | --- |
| Amazon Bedrock (Converse API) | Every model call, Claude Sonnet 4.5 in `us-east-2`, with prompt caching. About $0.80 per case. | Live (`src/tangible/agent/llm.py`) |
| Amazon S3 Object Lock + AWS KMS | Raw evidence store, write-once, encrypted per tenant | Built and tested (`src/tangible/evidence/s3.py`); the hosted demo uses a volume |
| AWS Secrets Manager | Connector tokens | Built (`src/tangible/secrets/`) |

## Flex Challenge: "Does the project leverage FinTech?"

Yes. Tangible is lending infrastructure: it takes a small business from invitation to a credit
recommendation.

| FinTech capability | Where |
| --- | --- |
| Borrower onboarding: lender invites, a versioned request catalog, read-only account connections (33 simulated providers), document upload | `src/tangible/onboarding/`, `web/`, `/demo/onboarding` |
| Financial ledger: orders, refunds, payouts, bank transactions, invoices, inventory, balances. Rows are never overwritten, and "as of a date" filters on when data was observed, so backtests can't see the future | `src/tangible/ledger/types.py`, migrations in `src/tangible/db/migrations/` |
| Bank and commerce data connectors (Plaid, Rutter) | `src/tangible/connectors/` (adapters written; the demo uses synthetic data) |
| 22 integrity checks: orders vs payouts vs bank deposits vs stated revenue, completeness, tampering (round-number, duplicated, back-dated or revised rows), uploaded statements vs the ledger, bought reviews | `src/tangible/checks/` (`reconciliation.py`, `tampering.py`, `documents.py`, `intangibles.py`) |
| 39 versioned pure metrics: revenue momentum, refund rate, inventory days, payables, cash runway, repeat-customer share | `src/tangible/metrics/` |
| Lender credit policy as data: knockout → bands → intangibles uplift (at most one notch) → review cap → limit → tariff stress (limit can only go down) | `policies/default.yaml`, `src/tangible/policy/` |
| Tariff exposure: the margin and cash flow left after an enacted duty, and how much of the book each action touches | `src/tangible/metrics/systemic.py`, `src/tangible/checks/systemic.py` |
| Replay: any assessment re-runs under the policy and metric versions it was frozen with | `src/tangible/assessment/`, `tangible-replay` |
| Backtest: runs a loan cohort as of each decision date and reports loss by band, AUC/KS per metric and finding lift | `src/tangible/backtest/`, `tangible-backtest` |
| Multi-tenant security: Postgres row-level security on every tenant table; borrower accounts can't see cases, findings or assessments | `src/tangible/db/migrations/`, `src/tangible/workbench/services.py` |

On the synthetic 20-loan cohort, policy 0.7.0 gave band A and B loans 0 defaults out of 10, band C
3 of 5, and every hold it raised was a loan that defaulted (5 of 5). The labels were planted, so
this tests the plumbing and the checks' catch rates, not predictive power.

## General criteria

- **Value, impact and usefulness.** Small-business lenders say no or charge more when they can't
  see inside a business. Tangible turns days of analyst work into a signed report with every
  claim tied to a tool output, for about a dollar of model spend per case.
- **Innovation.** Agents choose what to investigate, but in code, not just in the prompt, they
  can't set a number or touch the decision. Lenders can then use the result for credit.
- **Realness and completeness.** A deployed web app with a lender dashboard, invites, borrower
  onboarding, a due-diligence workspace with a live agent feed, sign-off, a backtest page and
  multi-tenant auth. 735 test functions in 61 files; `make check` runs ruff, mypy and pytest.

## Verify it yourself (offline, no AWS account)

The Strands engine, its hooks and the agent team run against scripted model responses, so they
need only Postgres:

```bash
make install
make db-up
uv run pytest -q tests/test_strands_engine.py tests/test_agent_loop.py tests/test_intangibles.py
```

Expected: `115 passed` (about 10 seconds). `make check` runs the full suite plus lint and types.

Live Bedrock run and the 90-second demo: see "The agent team, on Strands Agents" in the
[README](../README.md#the-agent-team-on-strands-agents) and [demo-script.md](demo-script.md).

## Known limits

- Live agent runs happen locally; the hosted API has no Bedrock credentials, so the hosted demo
  shows runs made beforehand ([deploy.md](deploy.md)).
- Plaid and Rutter adapters haven't been run against live accounts. Onboarding connections are
  simulated.
- The backtest cohort is synthetic, with planted labels. A real backtest needs a lender's history.
- Claude 5.x on Bedrock is blocked by an account quota, so live runs use Claude Sonnet 4.5.
