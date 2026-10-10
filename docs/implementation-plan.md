# Tangible — Implementation Plan

Oct 10, 2026 · companion to [build-plan.md](build-plan.md)

How we turn the build plan into code. An Opus coordinator owns the contracts, writes the
briefs, reviews every diff against the invariants, and merges. Sonnet subagents do the
implementation, one work package each, in their own git worktree.

## Where we are

| Area | State |
| --- | --- |
| Schema (`0001_init.sql`) | Done: 4 layers, RLS, immutability triggers, model-finding citation check. 5 DB tests pass. |
| Ledger types | `Order`, `Refund`, `Ledger`, `known_as_of`. No payout, bank, invoice, inventory or balance types yet. |
| Metrics | Registry + `net_revenue` v1, `refund_rate` v1. |
| Checks | Registry and `FindingDraft` only. No checks. |
| Policy | Loader (version + hash). No evaluator. `default.yaml` uses refund rate only. |
| Agent | `ModelClient` Protocol only. |
| Data | UCI loader done. `synthetic.py` generates the full footprint and 5 plants, but **another session is editing it right now**: mypy shows 3 errors, ruff 1, 1 test fails (`test_clean_run_reconciles`: late-payables share). |
| Repo | No commits yet. Worktrees need a base commit. |

Missing: ingest into Postgres, 16 metrics, ~16 checks, policy evaluator, assessment freeze and
replay, agent tools, agent loop, document reader, backtest harness, workbench, connectors.

## How the work runs

**Coordinator (Opus, this session).**
1. Does Wave 0 itself, or reviews it line by line. Wave 0 fixes every interface the later
   waves depend on, so it can't be split up.
2. Writes a brief for each work package (template below) and dispatches Sonnet agents with
   `isolation: "worktree"`, up to 5 at a time, all in one message.
3. Reviews each returned diff for: files outside the agent's ownership, invariant breaks (see
   checklist), tests that only test themselves, and `make check` output. Re-dispatches once
   with a tighter scope if needed; a second failure gets reported to the user as a blocker.
4. Merges in dependency order, runs `make db-up && make check` on the merged tree, and commits.
   Nothing starts on a later wave until the earlier one is green on `main`.

**Subagents (Sonnet).** Each one gets a single work package, an explicit list of files it owns, the
frozen interfaces it must use but not change, and an acceptance command. It must run
`make check` and report back: what changed, evidence, caveats, exclusions.

**Conflict rules.**
- File ownership is exclusive within a wave. Shared registration files (`metrics/__init__.py`,
  `checks/__init__.py`, `agent/tools/__init__.py`) are pre-wired in Wave 0, so agents only fill
  in their own modules.
- Migration numbers are assigned in this plan, never chosen by an agent.
- Only the coordinator adds dependencies to `pyproject.toml`. A brief lists any deps the package
  needs, and the coordinator adds them before dispatch.
- `src/tangible/datasets/` belongs to the in-flight synthetic work and stays out of every
  brief until that work lands.

### Brief template

```
Task: <work package id + one-line goal>
Read first: CLAUDE.md, docs/build-plan.md §<section>, docs/implementation-plan.md §<WP>
You own (create/edit only these): <paths>
Frozen — use, don't change: <modules/types>
Spec: <rules, thresholds, versions, edge cases>
Tests to write: <named cases, incl. time-rule and plant cases>
Acceptance: `make db-up && make check` passes; <specific pytest -k selectors>
Report: files changed + why, test output, caveats, what was excluded. Note any ambiguity and the choice you made.
```

### Review checklist (every diff)

- No row is filtered on `occurred_at` without first going through `known_as_of` / `Ledger.known`.
- No released metric or check version was edited (a new formula gets a new version).
- New tables are in a new numbered migration, with RLS and append-only triggers where needed.
- No code outside `agent/llm.py` imports a model SDK.
- No agent-facing code sets a band, limit, or severity outside a check or policy.
- Borrower/web text reaches the agent only as quoted data.
- Decimal for money everywhere; no float in a metric or check path.

## Wave 0 — baseline and contracts (serial, ~1 agent + coordinator review)

**WP0.1 Baseline.** Wait for the synthetic.py work to land (or confirm with its owner), then
get `make check` green and make the initial commit. Worktrees can't be created until that
commit exists.

**WP0.2 Ledger contract** (`ledger/types.py`, `ledger/evidence.py`). These become frozen after
this WP:
- `EvidenceRef(pull_id: str, locator: str)`, string form `"{pull_id}#{locator}"`.
- Row types for every ledger table, matching `0001_init.sql`: `Payout`, `BankTxn`, `Invoice`,
  `InventorySnapshot`, `AccountBalance` (plus `channel`/`currency`/`evidence` on `Order`,
  `evidence` on `Refund`). New fields get defaults so existing tests keep passing.
- Case facts the checks need: `Connection(source, granted_at, revoked_at, last_success_at,
  observed_at)` and `Application(stated_revenue_90d, own_account_names, owner_names, observed_at)`.
- `Ledger` gains a tuple per table plus `connections` and `application`, and
  `Ledger.known(as_of) -> Ledger`, which applies `known_as_of` to every table. This is the one
  entry point metrics and checks use.

**WP0.3 Finding and set contracts** (`checks/registry.py`, `metrics/sets.py`, `checks/sets.py`).
- `FindingDraft` gains `direction` (default `raises_risk`) and `facts: Mapping[str, str]` (the
  values the rule saw, for "Band C because…"-style explanations). `evidence_refs` holds
  `EvidenceRef` strings.
- `METRIC_SETS = {"ms-1": ((key, version), ...)}` and `CHECK_SETS = {"cs-1": ...}`. Policy and
  assessments pin a set id, never loose versions.
- Pre-create empty modules and wire imports: `metrics/{health,momentum,series}.py`,
  `checks/{reconciliation,completeness,tampering,documents}.py`.

**WP0.4 Migration `0002_case_agent.sql`.** All tables tenant-scoped with RLS (reuse the 0001
block); `*` = append-only trigger.
- `case` (borrower, as_of, loan_amount, budget_steps, budget_usd, assigned_analyst, status)
- `agent_plan`* (case, version, body jsonb, approved_by, approved_at)
- `tool_call`* (id, case, tool, tool_version, args jsonb, result jsonb, result_sha256,
  started_at, duration_ms, error)
- `model_call`* (case, model_id, prompt_key, prompt_version, input/output tokens, cost_usd)
- `borrower_question`* (case, body, drafted_by_tool_call). Approval goes through `underwriter_action`.
- `audit_log`* (actor, action, object, at)
- `finding`: add `case_id`, `verified bool`. `underwriter_action.action`: add `accept`,
  `reject`, `approve_plan`, `approve_question`, `sign`; add nullable `case_id` and
  `question_id` (relax the num_nonnulls check to "exactly one target").
- **A finding's review state comes from its latest `underwriter_action`; `finding.status`
  is never updated.** This keeps review append-only.

Migration numbers reserved for later waves: `0003` workbench auth/users, `0004` connectors.

**Exit:** `make check` green, contracts committed. Coordinator writes the Wave 1 briefs.

## Wave 1 — deterministic tools (5 parallel Sonnet agents)

Everything in this wave is pure or close to it, and is tested on hand-built `Ledger` fixtures
plus synthetic CSVs.

| WP | Owns | Builds | Acceptance (besides `make check`) |
| --- | --- | --- | --- |
| **1A Ingest + repo** | `ingest/`, `db/repo.py`, `db/session.py` | Load a synthetic output dir (CSV + `evidence_pull.csv`) into Postgres as one borrower. `tenant_session(tenant_id)` sets role and `app.tenant_id`. `load_ledger(conn, borrower_id) -> Ledger` with all revisions (time filtering stays in `Ledger.known`). Local evidence store with an S3-shaped interface (`put` is write-once, sha256 verified). CLI `tangible-ingest`. | Round trip: CSV → DB → `Ledger` equals the CSV parsed directly. Re-ingest is idempotent on (pull sha256). RLS test: tenant B can't load A's ledger. |
| **1B Health metrics** | `metrics/health.py` (keep v1s untouched) | gross_margin_est, repeat_customer_share, days_of_inventory, aged_stock_share, cash_runway, payables_discipline, payout_lag. Each is v1, uses `Ledger.known`, returns `None` when inputs are missing. | Per metric: hand-computed fixture, `None` case, observed-after-as_of invisibility, revision case. |
| **1C Momentum** | `metrics/momentum.py`, `metrics/series.py` | Weekly series builder (13w / 26w), 13w slope, 30-vs-prior-30 change, break flag (>2σ from 26w mean), YoY when 12+ months. Momentum metrics for revenue, refund rate, payout lag, days of inventory, payables days, ad spend ÷ revenue. Series points are each computed `as_of` their own week end (no look-ahead). | Look-ahead test: appending future-observed rows doesn't change any past point. Slope/break on synthetic series with known answers. |
| **1D Reconciliation checks** | `checks/reconciliation.py` | payouts_vs_deposits, books_vs_processor, orders_vs_payouts, inventory_flow, payables_vs_bank, stated_vs_observed, with the severities in build-plan. Thresholds are module constants per version. | Clean synthetic: no review/hold. `hidden_reserve` → payouts_vs_deposits hold; `phantom_payouts` → orders_vs_payouts review. Every finding carries evidence refs. |
| **1E Completeness + tampering** | `checks/completeness.py`, `checks/tampering.py` | time_gaps, stale_connection, partial_coverage, short_history, revoked_mid_review, backdated_edits, round_number_pattern, self_transfers_as_revenue, plus **unexplained_recurring_debit** (catches the `undisclosed_lender_debit` plant). | `partial_coverage` plant → hold; `self_transfer_revenue` → review; `undisclosed_lender_debit` → review. Clean run: info at most. |

Also in this wave (6th slot, or once the first agent finishes):

| **1F Policy evaluator** | `policy/evaluate.py`, `policy/schema.py`, `policies/default.yaml` (bump to 0.2.0) | Validate the policy file (a typed schema; reject unknown keys). Evaluate in order: knockout (open hold, using effective review state) → bands A–D from metric thresholds → limit = band share × net_revenue, capped by band and history length. Returns `PolicyResult(band, limit, rules_fired=[{rule, saw, threshold}])`. A pure function of (policy, metric values, findings + review state). | Each rule path has a test, including hold → `band=None, limit=None`, missing metric → rule recorded as "not evaluated", same inputs → identical result + hash. |

**Exit:** all 5 plants caught by rule checks on synthetic data; clean data raises no hold. The
coordinator adds `tests/test_plants_matrix.py` (plant × expected check × severity) as the
regression gate for every later wave.

## Wave 2 — freeze, replay, agent tool layer (4 parallel agents)

| WP | Owns | Builds | Acceptance |
| --- | --- | --- | --- |
| **2A Assessment** | `assessment/` | `run_baseline(case)` computes the metric set + check set at `case.as_of`, writes `metric_value` (with `input_hash`) and rule findings. `freeze(case, analyst)` runs policy and writes `assessment` + `assessment_item` + `rules_fired`, plus an `evidence_set_hash` over the sorted (pull sha256) used. `replay(assessment_id)` reloads by `observed_at`, reruns the pinned versions, and diffs. CLI `tangible-replay --sample N` for the nightly job. | Replay of a fresh assessment matches exactly. Adding later-observed rows doesn't change the replay. Tampering with a ledger row (as superuser, in test) makes replay fail loudly. |
| **2B Tool runtime** | `agent/tools/runtime.py`, `agent/tools/{ledger,library,findings,questions}.py` | Tool registry with JSON schemas. Every call is logged to `tool_call` before its result is returned. Tools: `query_ledger` (read-only SQL against an **in-memory SQLite snapshot built from `Ledger.known(case.as_of)`**, so the agent never touches Postgres; row cap + timeout), `compute_metric`, `run_check` (same registry and sets as policy), `record_finding` (rejects missing/unknown tool_call_ids; `origin=model`), `draft_borrower_question`. | Snapshot rows == `Ledger.known` rows. SQL writes/ATTACH/pragma are rejected. `record_finding` with a fake call id → error. Every call has a `tool_call` row. |
| **2C Analysis sandbox** | `agent/sandbox.py`, `agent/tools/analysis.py` | `run_analysis(code)`: a subprocess `python -I` with an empty env and a temp dir holding the snapshot (SQLite file), plus rlimits (CPU, address space, file size) and a wall timeout. Network is blocked by a socket guard locally; production runs it as a no-egress Fargate task (documented, not built). Code and stdout are saved in the `tool_call`. | Timeout, memory cap, network attempt, and file-escape attempts all fail cleanly. A simple pandas-free aggregation returns the right result. |
| **2D Model client + verifier** | `agent/llm.py`, `agent/verify.py`, `agent/prompts/` | `BedrockClient` (Converse API, model id from config; boto3 is the only SDK import, and only in llm.py). `ScriptedClient` for tests. Every call is logged to `model_call` with tokens + cost. Prompt files are versioned (`name@vN.md`, hash recorded). `verify_numbers(text, cited_results)`: every number in a finding must appear (within rounding) in a cited tool result, or the finding is stored `verified=false`. | `grep` test: no `boto3`/`anthropic` import outside llm.py. Verifier catches an invented number, tolerates 9.4% vs 0.0940, and handles currency formatting. |

**Exit:** a scripted (no real model) case runs end to end: baseline → tool calls → model
findings → freeze → replay equal.

## Wave 3 — agent loop, documents, backtest (4 parallel agents)

| WP | Owns | Builds | Acceptance |
| --- | --- | --- | --- |
| **3A Investigation loop** | `agent/loop.py`, `agent/prompts/investigate@v1.md`, `agent/prompts/plan@v1.md` | The steps: plan (stored, waits for `approve_plan`) → baseline → investigate (tool loop under step + USD budget from case tier) → record. Tool results go back as quoted data blocks. The loop stops on budget and records why. No tool can send anything outside. | With ScriptedClient: budget stop, unapproved-plan block, finding without citations rejected, unverified number flagged. |
| **3B Document reader + doc checks** | `agent/documents.py`, `checks/documents.py`, `tests/fixtures/poisoned/` | `read_document(pdf)`: text extraction → separate model call with **no tools**, schema-constrained fields out (statement period, account, opening/closing, txn lines). Extracted values become a ledger-shaped source that goes through the regular reconciliation checks. Checks: document_mismatch (vs bank feed → hold) and document_file_traits (PDF metadata: modified after creation, producer unlike the bank's → review). Poisoned set: ≥10 PDFs/pages with injection text. | Poisoned set: the reader's output never contains instructions, and agent decisions on a poisoned case match the clean twin (ScriptedClient + one live-model smoke test behind a flag). Forged-statement fixture → hold. |
| **3C Web lookup** | `agent/tools/web.py` | A provider interface with an allow-list of domains and per-source adapters (stubbed with fixtures: reviews, marketplace standing, UCC, court records, trademarks). Content is returned as quoted data with its source URL. | Off-list URL rejected. Output is wrapped as data. No live network in CI. |
| **3D Backtest harness** | `backtest/` | Cohort file (`borrower_key, data_dir, decision_date, loan_amount, outcome, loss_amount`). For each loan: ingest → case `as_of = decision_date` → baseline (+ agent if enabled) → freeze. Report (Markdown + JSON): loss rate by band, per-metric AUC/KS against outcome, findings that came before defaults, plant catch rate, false alarms per case, spread across 5 agent runs, cost and steps per case. Synthetic cohort builder: seeds × plant combos, labelled `planted ⇒ bad`, **clearly marked synthetic in the report**. | A synthetic cohort runs in CI (small). The report renders. A time-leak test: moving the decision date earlier never uses later-observed rows. |

**Exit = the build-plan "week 11" gate on synthetic data.** The real gate needs Flex.one's
historical file, run through 1A (or a new CSV adapter if their format differs).

## Wave 4 — workbench and connectors (3 parallel agents)

| WP | Owns | Builds |
| --- | --- | --- |
| **4A Workbench API** | `api/`, migration `0003` | FastAPI app: cases (scoped to the assigned analyst), plan approve/edit, findings review (accept / reject / send back with a question), borrower-question approval, sign (→ freeze), trace view (finding → tool calls → evidence ref → raw line). Audit log on every view and export. Auth: OIDC stub with lender_underwriter, credit_admin and analyst roles. |
| **4B Workbench UI** | `web/` | Server-rendered (Jinja + htmx) screens for the API above. No SPA. |
| **4C Connectors + evidence store** | `connectors/`, migration `0004` | Rutter-or-Codat and Plaid adapters. Pull → raw JSON to the evidence store (S3 Object Lock in governance mode; the local store from 1A in dev) → evidence_pull → normalised ledger rows with locators. Consent and revocation drive the `connection` rows. Tokens go through a secrets interface (Secrets Manager in prod), never logged. Tested on recorded sandbox fixtures. |

Not in this plan, matching the build-plan exclusions: AWS infra-as-code, Akamai config,
SOC 2 controls, production hardening, and connectors beyond the unified API and Plaid.
These can come later as their own WPs.

## Sequencing summary

```
W0  baseline + contracts + 0002            serial            ── gate: make check green, first commit
W1  1A 1B 1C 1D 1E (+1F)                   5–6 parallel      ── gate: plants matrix passes
W2  2A 2B 2C 2D                            4 parallel        ── gate: scripted case end-to-end + replay
W3  3A 3B 3C 3D                            4 parallel        ── gate: synthetic backtest report
W4  4A 4B 4C                               3 parallel        ── gate: analyst signs a case in the UI
```

About 20 Sonnet dispatches plus re-dispatches. Waves 1–3 carry the backtest milestone; Wave 4
is the live-case milestone.

## Open items

- **synthetic.py is being edited in another session.** Wave 0 waits for it to land. Owner: whoever is editing it.
- **Ambiguities, and the defaults this plan picks:**
  - The snapshot is in-memory SQLite built from `Ledger.known`, not Postgres views. This keeps
    the time rule in one place.
  - Review state is derived from actions, never stored on `finding`.
  - Opex shows up as payables in synthetic data (ADS-*). The payables checks and metrics must
    exclude zero-term invoices, or the policy needs a rule for them. To be confirmed once
    synthetic.py settles.
- **Real model.** Wave 3 needs Bedrock access (model id + region) for the live smoke test and
  the 5-run spread measure. Everything before that runs on ScriptedClient.
- **Backtest data.** The real gate is blocked on the Flex.one historical file, as the build plan says.
