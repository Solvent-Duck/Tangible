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

**Status (Oct 10): items 6–9, 10b, 11 and 11b are merged on `demo-readiness`** (`make check` 855 passed). Item 10 (stated vs observed) is not started; it's still your call.

- **6 Distinct borrowers:** each borrower has its own profile: account, processor (Shopify/Stripe/Square/SumUp), owner and lender names, bank, landlord and suppliers.
- **7 Documents:** `tangible-demo statements` writes genuine, forged (→ `document_mismatch` hold), edited (→ `document_file_traits` review) and injection PDFs from the ledger.
  - Rebase now reads late uploads, so the document checks can fire. Before, they never could.
  - "Upload and rebase" is one step while a case is open or planning.
- **8 Web lookup:** a fixture provider (`TANGIBLE_WEB_PROVIDER=fixtures`) for the five borrowers:
  - Marlow Lane: Fernbank Funding court judgment and charge.
  - Brightwell: director change, plus a review page with injected instructions.
  - Oakden: Amazon storefront.
  - Kestrel: refund complaints.
- **9 Connections:** Brightwell's SumUp feed is stale (review). Oakden's bank was revoked 3 days before as_of (hold).
- **10b:** `books_vs_processor` v2 in check set `cs-4`.
- **11:** `investigate@v3`. The agent is told whether each finding verified. Its wrap-up message is stored and shown.
- **11b:** policy 0.3.0. Agent holds block only once accepted. Replay uses the policy file the assessment was frozen under.

Not yet tested with a live model: v3's verified rate, the web lookups, and reading documents at rebase. All of these need a paid `--run-agent` pre-run.

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
