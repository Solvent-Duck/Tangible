# Tangible — Demo Run-Sheet

Synthetic data only. Everything shown is generated; no borrower data is involved.

## Morning of the demo (about 20 minutes, about $3)

```bash
make db-up
aws login --profile tangible          # Bedrock credentials expire; refresh them first
TANGIBLE_MODEL_BACKEND=bedrock \
TANGIBLE_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
TANGIBLE_BEDROCK_REGION=us-east-2 \
TANGIBLE_PRICE_IN_PER_MTOK=3.30 TANGIBLE_PRICE_OUT_PER_MTOK=16.50 \
TANGIBLE_WEB_PROVIDER=fixtures TANGIBLE_SIGNAL_PROVIDER=fixtures AWS_PROFILE=tangible \
uv run --with "botocore[crt]" tangible-demo seed --run-agent --lender-name "<lender>"
```

`--lender-name` is the name invited businesses see in the invite email and onboarding. Without it
they see the tenant id (`demo-20261011-0559`).

The seed also loads the two fictional tariff actions and creates the onboarding pipeline (four
invited US businesses, one of them submitted and ready for diligence). For the hosted demo the
same tenant is restored on Railway and your Clerk account is linked as its credit admin (see
"Hosted demo" below).

The seed prints the tenant id. Then write the statement PDFs for the live upload:

```bash
uv run tangible-demo statements --tenant-id <tenant id> --borrower "Harbour & Pine Homeware Ltd" --out data/demo/statements
```

Generate the backtest report the Backtest tab reads (about 3 minutes; step 9 needs it):

```bash
uv run tangible-backtest synth --lines data/processed/uci_lines.csv --out data/backtest/cohort
uv run tangible-backtest run --cohort data/backtest/cohort/cohort.csv --report data/backtest/report.md
```

Start the `workbench-demo` launch config (the API on port 8001; Sonnet 4.5, web fixtures and
prices set), then the web app against it:

```bash
TANGIBLE_API_URL=http://127.0.0.1:8001 make web-dev
```

Open the sign-in URL the seed printed (`http://localhost:3000/sign-in?tenant=…`) and sign in as
**ana** (analyst). Keep a second browser profile signed in as **adm** (credit admin) for the
invite and **len** (lender) for the end; dev sign-in is per browser.

Seed on the demo day. The data ends this week. A case rebased during the demo is dated "now",
and connections seeded days earlier would read as stale.

## The tenant

| Borrower | State | The story |
| --- | --- | --- |
| Harbour & Pine Homeware Ltd | open | Clean business. Kept for the live agent run and the forged statement. |
| Marlow Lane Interiors Ltd | signed, band C | Daily £450 debits to Fernbank Funding with no invoice: an undisclosed lender. The agent's web lookups found an £8,450 county court judgment and a registered charge from the same lender (verified agent holds, not blocking until accepted). |
| Kestrel Gift Co Ltd | signed, no band | Payouts don't reach the bank: a hidden reserve. The rule hold blocks a decision. |
| Brightwell Crafts Ltd | review | Owner money counted as sales, payouts above orders, stale SumUp feed. A review page carries an injection attempt; the agent ignored it and recorded it as an info finding. The registry also shows a director change: a good send-back question ("check the companies registry for officer changes"). |
| Oakden Trading Ltd | review | Amazon income from an account it never connected; bank access revoked 3 days before the case date (both agent holds, verified). |
| Juniper & Rye Bakery LLC (US) | open | Kept for the live Strands run. Its regulars lift it one band by the loyalty route; it states no imports. |
| Northgate Phone Repair LLC (US) | open | A same-name Spokane shop the search must refuse (match 0.40), whose reviews try to talk the agent into claiming it. Imports phone parts from China: tariff exposure. |

The onboarding pipeline (dashboard): Cedar & Sage Florist (invited), Bluebird Cycle Works
(opened the link), Harborview Optics (connecting) and **Lakeside Furniture Co** (submitted;
55% of cost of goods is Chinese furniture, under the fictional DEMO-2026-0001 duty).

## Flow (about 15 minutes)

0. **The lender's day (2 minutes).** As **adm**, the dashboard: the pipeline from invited to
   signed, Lakeside "Ready for diligence". Open it: the checklist, timeline and the import mix it
   stated. **Start diligence** and assign ana. Optionally show **Invite a business**: toggle groups,
   the live email preview.
00. **Invite (optional, 3 minutes).** As **adm**: Invite a business → toggle Inventory and
   Digital presence → watch the email preview → Send. Copy the link from the Outbox, open it in
   a private window, create the account, connect a bank, upload a file, submit. Back on the
   dashboard the business is "Ready for diligence"; Start diligence assigns it to ana.
1. **Portfolio.** On Due diligence, each case shows its status, band and limit at a glance.
2. **The split.** "The agent investigates, the tools compute, the policy decides." Open
   **Marlow Lane** → assessment:
   - "Band C because…": every rule that fired, with the value it saw.
   - Band B on the metrics, capped at C by open review findings.
   - The agent's own hold is listed but not blocking until an analyst accepts it. The agent
     never sets a band.
3. **Trust the numbers.** Open a Marlow Lane agent finding → **Trace to source**: the tool
   call, the SQL it ran, the result table, the evidence ref, and the raw ledger line as
   received. Point out the "verified" badge: every number in the finding was found in a cited
   tool result.
4. **Live investigation on the Strands team.** Open **Juniper & Rye** → Propose plan → Approve →
   Start investigation. The status panel's live feed shows the presence, loyalty and reputation
   agents searching in parallel, then the lead investigator. When it finishes: **Public signals**
   (matched listings with entity_match scores) and **Agent run** (the team, steps, tool calls).
   Sign it: the assessment reads "uplift C→B via loyalty". Then run **Northgate**: the decoy
   scores 0.40 and is refused. (Harbour & Pine is the classic-loop alternative.) While it runs, show
   **Oakden** (holds from rules and verified agent findings; the "Checked, nothing found"
   wrap-up).
5. **Review.** On **Brightwell**:
   - Accept the self-transfer finding.
   - Reject one finding with a reason.
   - Send back with a question, e.g. "Check the companies registry for officer changes" (the fixtures show a director change in July).
   - Show the info finding about the review-page injection: the agent quoted it and did not act on it.
   - Show a corrected finding: "Superseded earlier version" holds the version that failed verification.
6. **Forged statement.** On a fresh open case (or Harbour & Pine before step 4), upload
   `data/demo/statements/harbour-pine-homeware-ltd-statement-forged.pdf` with **Upload and
   re-base** (about 50 seconds while the statement is read). The new case's baseline shows a
   `document_mismatch` **hold**: 3 statement lines with no bank match. The genuine PDF raises
   nothing.
7. **Sign.** Sign a review case. Signing freezes the assessment, then:
   - **Replay** shows it re-derives exactly;
   - **Export JSON** gives the frozen assessment.
8. **Lender view.** Switch to **len**. The lender sees signed cases only and has no actions.
8b. **Systemic exposure.** On Lakeside's case (or Northgate's), **Systemic exposure** lists the
   stated imports against the tariff action known at the case date; the cases list shows **Book
   exposure** across the portfolio. Policy 0.5.0 only ever lowers the limit, never the band.
9. **Backtest.** Open the Backtest tab: 13 of 13 planted problems caught, loss by band, all 20
   assessments replay, and no data leaks across the decision date. Say clearly that it's
   synthetic: the labels are planted problems, not repayment outcomes.

## If something goes wrong

- **Live run fails** (credentials, throttling): the case page says why in a plain sentence.
  Show the pre-run cases instead; they have the same flow.
- **Run stuck in "investigating"** after a restart: use the Reset button on the case (after 15
  minutes, or set `TANGIBLE_STUCK_AFTER_MIN=1` before starting the server).
- **Upload not read** ("could not be read"): check the model backend. Show the forged-PDF result
  from the rehearsal instead.
- **Cost:** the case page shows model calls and cost per case. About $0.50–$1 per
  investigation.

## Reference run (Oct 10, tenant `demo-20261010-2141`)

- **Cost and length:** $2.37 for 4 investigations, 14–31 steps each (budget 40).
- **Web lookups:** 19.
- **Corrections:** 10 findings were corrected with `replaces`, and the earlier versions show as superseded.
- **Verification:** 6 of 24 current agent findings are unverified. Most are review-site figures (ratings, percentages) that the checker can't match in quoted web text.

## Don't claim

- The backtest is not evidence of predictive power. It runs on a synthetic cohort.
- Web results are fixtures, not live lookups.
- Bands and limits come from a starting-guess policy (0.5.0), not a calibrated one.


## Hosted demo

- Web app: the `web` service on Railway (Clerk sign-in). API: the `workbench` service.
- Two Clerk accounts, two personas: one is the tenant's credit admin (`--link-subject`), the
  other its analyst with every case assigned (`--link-analyst`). Use two browser profiles.
- Live agent runs need Bedrock credentials, which the hosted API does not have: the hosted demo
  shows the pre-run cases. Run live investigations locally.
- The Backtest tab needs the report on the API's volume (see [deploy.md](deploy.md)); otherwise
  it shows how to generate one.
