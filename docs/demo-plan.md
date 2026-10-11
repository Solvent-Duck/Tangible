# Tangible — Demo Readiness Plan

Oct 10, 2026 · companion to [implementation-plan.md](implementation-plan.md)

Goal: a live demo on synthetic data that shows every feature, with fallbacks if a live model
call fails.

## Where we are

- `make check` is green: 669 tests.
- The full flow works end to end: plan → approve → investigate → review → sign → assessment
  ("Band B because…") → replay → trace.
- Claude Sonnet 4.5 on Bedrock has run live. It found both problems planted in synth-000, cost
  about $1.50 and took about 3 minutes per case. The Claude 5.x models are still blocked by
  quota.
- **Gaps:**
  - No single setup command, and nothing creates users.
  - The ingest command and the workbench use different evidence folders by default.
  - The data is dated 2011.
  - Documents, web lookup and connection checks have no synthetic data.
  - Raw values show up in the UI.
  - 12 of 14 Sonnet findings are marked unverified because window lengths ("90 days") count as
    numbers that need a citation.

## P0: without these the demo breaks or looks broken

| # | Item | Notes |
| --- | --- | --- |
| 1 | `make demo` / `tangible-demo seed` | Fresh demo tenant; named borrowers from the partitioned synthetic merchants, each with one plant story; analyst, credit_admin and lender users; cases at different stages; prints the login URL. The workbench and ingest use the same evidence folder. |
| 2 | Dates moved to 2026 | Shift the order lines by whole weeks before generating, so the data ends shortly before the demo date and uploads don't need a rebase. |
| 3 | Number verifier fix | Ignore window phrases ("90 days", "13 weeks", "2σ"). Match a decline written as a positive percent against a negative value. Show the unmatched numbers on the finding. |
| 4 | Agent runs reliable on stage | Show investigation failures on the case page. Add an audited reset for cases stuck in `investigating`. Set Sonnet 4.5 prices in the demo config. Use Bedrock prompt caching. |
| 5 | Pre-run fallbacks | Run the agent on every staged case ahead of time. Refresh `aws login` before the demo (it needs `botocore[crt]`). |

**P0 status (Oct 10):** done on `demo-readiness` (`make check` 729 passed).
- **Pre-run:** tenant `demo-20261010-1948`, Sonnet 4.5. Total $2.70 for 4 cases ($0.34–$0.94 each). Prompt caching served 3.7M of the 3.9M input tokens.
- **Plants:** the agent found every plant: the YRDEAL lender debit (verified), Amazon not connected, owner self-transfers and the payouts gap.
- **Signed:** Marlow Lane at band C, limit £43,411.62 (band B on metrics, capped by its review findings).
- **Fixed:** a verifier crash found in the pre-run (it would also have broken the case page).
- **Still open:** most agent findings remain unverified because Sonnet computes percentages and differences in its head instead of with a tool (item 11).

## P1: show every feature

| # | Item | Notes |
| --- | --- | --- |
| 6 | Distinct borrowers | Each borrower gets its own counterparty names, processor and account instead of "J SMITH / SHOPIFY / GB-OPS-001". |
| 7 | Documents | Generate PDF statements from a borrower's own ledger: genuine, forged (→ `document_mismatch` hold), edited after creation (→ `document_file_traits`), and one with injected instructions. |
| 8 | Web lookup | A fixture provider in demo mode with entries for the demo borrowers: reviews, company registry, one court-record hit and one injection page. Today the tool always fails in the workbench. |
| 9 | Connections | The setup writes `connection` rows, including one revoked mid-review, so `stale_connection` and `revoked_mid_review` fire. |
| 10 | Stated vs observed | The database loader hard-codes `applications=()`, so this check never fires. Needs an application table (migration 0007), or leave it out of the demo. |
| 10b | `books_vs_processor` false review on clean data | When the last data day is around the 5th to 9th, the previous month is judged before its last payouts have settled. Every merchant trips this about 9–11 weeks a year. The seed works around it by moving `end_on` back up to 4 weeks. The real fix is a v2 that waits for settlement. |
| 11 | Prompt `investigate@v3` | (a) Compute every derived number (ratio, difference, %) with `run_analysis` before writing it, so it can be cited. In the pre-run, about 80% of unmatched numbers were this. (b) Don't record "all clean" findings, or show them in a separate "checked, nothing found" section. |
| 11b | Decide: should an agent hold block the decision before review? | Policy knockout counts any open `hold`, including agent findings that are unverified and unreviewed. In the pre-run, Brightwell was knocked out by an unverified agent hold. Option: agent holds block only once an analyst accepts them (needs a new policy version). |

## P2: polish

12. Number formatting: percents, currency, 2 decimal places, readable metric labels and dates.
13. Case page: borrower name as the heading, limit shown next to band, plan as a checklist
    instead of a JSON box, go to the case after creating it.
14. Dev login: pick a tenant instead of pasting a UUID.
15. Trace and assessment pages: show tool arguments and results as tables; clean up evidence refs
    and the knockout row.
16. Backtest summary page in the workbench, labelled synthetic.

## Demo flow

Portfolio list → fresh case: plan, approve, live agent run → review findings (accept, reject,
send back with a question) → trace a number to its raw line → upload a forged PDF, which
triggers a hold → sign → assessment explains "Band B because…" → replay matches → backtest
summary.

## Open decisions

- Demo date. If it's under 2 days away, do P0 plus items 6 and 7 only.
- Model: Sonnet 4.5 (works now), or accept the Bedrock agreement for Sonnet 4.6 or Opus 4.6.
- Stated vs observed: build migration 0007, or leave it out of the demo.
