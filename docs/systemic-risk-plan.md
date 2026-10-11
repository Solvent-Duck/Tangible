# Tangible — Systemic Risk Plan

Oct 11, 2026 · companion to [build-plan.md](build-plan.md) and [intangibles-plan.md](intangibles-plan.md) · status: draft for review

## Summary

A borrower can be healthy today and still be exposed to something outside its control. An online
homeware shop that imports most of its stock from one country can lose its margin overnight when
a new tariff lands. A bakery can be squeezed by a flour shortage. Today Tangible sees none of this.
Every metric looks at the borrower's own records only.

This plan adds a **systemic exposure** overlay. Tangible does not forecast events. It pulls
enacted or announced, dated facts about the world from public sources: tariff actions, duty
rates, import prices, supply-chain pressure. It measures how exposed each borrower is from its own
data, and computes a **stressed** view of the numbers the policy already uses. Policy 0.5.0 can
then reduce the **limit**, not the band, when the stressed view is weak. The lender also gets a
**book exposure** view: how much of the portfolio a given tariff action touches.

The core split does not change: **the agent investigates, the tools compute, the policy decides.**
The agent may look up which actions apply and propose an HS mapping. It never estimates an
impact, a probability or a number. Every external fact is a stored, dated row. Every stress
number comes from a versioned pure function, and every limit change is a policy rule that records
the values it saw.

## v1 slice (hackathon), as built

Built on fixtures, US only:

- **Data.** Migration `0010_macro_signals.sql` adds a global `macro_signal` table: no tenant_id,
  append-only, readable by the app role, writable only by the owning role through
  `tangible-macro load`. It also adds `application.import_mix`, the stated imports as
  `{hs_chapter, country, share_of_cogs}` lines. Every ledger carries the macro rows, and
  `known_as_of` filters them by **publication** date. The rows are part of the ledger input hash.
- **Fixtures.** `src/tangible/macro/fixtures/demo_tariff_actions.json` holds two **fictional**
  notices in Federal Register shape, each titled "[Demo fixture, fictional]":
  - DEMO-2025-0007: +10% on HS 94 from CN, in effect since 2025, so already reflected in the margin.
  - DEMO-2026-0001: +25% on HS 82, 85 and 94 from CN, effective 2026-11-01.
- **Metrics `ms-4`:**
  - `import_share`
  - `tariff_rate_change`: only actions taking effect in (as_of − 90 days, as_of + 180 days]
  - `tariff_margin_shock`
  - `stock_buffer_months`
  - `stressed_margin`
  - `stressed_flow_margin`: added during the build, see below
  - `stressed_runway`: capped at 24 months
- **Checks `cs-6`:** `tariff_exposure` (review) and `import_share_unstated` (info).
- **Policy 0.5.0** (`default-0.4.0.yaml` is kept for replay). There are three stress rules; the
  first match wins:
  - runway ≤ 3 months → limit ×0.5
  - runway ≤ 6 months → limit ×0.75
  - the tariff turns monthly cash flow negative (`stressed_flow_margin ≤ 0` and
    `tariff_margin_shock ≥ 0.02`) → limit ×0.75

  The floor is 0.5, and the band never changes.
- **Workbench.** A "Systemic exposure" panel on the case page (stated imports, the new duty per
  line, the actions that count) and a "Book exposure to tariff actions" card on the cases list.
- **Demo.** The seed loads the fictional actions.
  - Northgate Phone Repair states 70% of COGS in HS 85 from CN. The tariff takes about 10% of its
    revenue and turns its monthly cash flow from about +8% of revenue to about −1.8%, so its
    limit is cut ×0.75 (114,182.91 → 85,637.18).
  - Juniper & Rye states no imports.
  - The UK borrowers state no mix, so their stress is not evaluated.

**Changes from the plan:**
- **The product-to-HS mapping table was not built.** The ledger carries SKU codes but no product
  descriptions, so v1 takes HS chapters from the stated import mix instead. `hs_mapping` and its
  confirm/reject actions are deferred.
- **`stressed_flow_margin` was added.** The synthetic demo borrowers all generate cash, so a
  runway-only rule never fires on them. "The tariff turns cash flow negative" is also a better
  test of debt-service capacity for a cash-flow lender.
- **There is no `macro_agent` yet.** Macro data is loaded by the trusted loader. Agents can read
  the effect through the metrics but cannot write macro rows.
- **Supply-pressure series (GSCPI) and the backtest wiring are not built.**
- **Replay caveat:** an assessment frozen before a macro row was loaded with an earlier
  publication date will not replay to the same input hash. Keying on `loaded_at` (ALFRED-style
  vintages) is a v2 item.

## Exposure has two halves

| Half | What | Where it comes from |
| --- | --- | --- |
| The event | Duty rate by HS code and country, before and after an action | USITC Harmonized Tariff Schedule (HTS) REST API |
| The event | The tariff action itself: what, which HS codes, effective date, publication date | Federal Register API (Section 301 / 232 / IEEPA notices); USTR Section 301 lists |
| The event | Import price pressure by product group | BLS import price indices (via FRED) |
| The event | General supply-chain pressure | NY Fed Global Supply Chain Pressure Index (monthly) |
| The event | Import volume by HS code and country (context for the book view) | Census International Trade API |
| The borrower | Share of cost of goods that is imported, and the main source countries | Application v3 field (self-reported, soft); later checked against supplier invoices |
| The borrower | Product mix as HS chapters | Versioned lookup from product types / invoice lines; the analyst confirms ambiguous ones |
| The borrower | Cushion | Existing metrics: `gross_margin_est`, `days_of_inventory`, `cash_runway` |

Stock already on hand absorbs part of a tariff, because it was bought at the old price. That is
why `days_of_inventory` belongs in the stress, and why the same tariff hits a lean drop-shipper
harder than a well-stocked shop.

## How it flows

```
public sources (tariff actions, HTS rates, price indices)
        │  dated, hashed; published_at becomes observed_at
        ▼
macro_signal rows (append-only, known_as_of)
        │
borrower exposure: import share (application) × HS chapters (versioned map, analyst-confirmed)
        │
        ▼
metrics ms-4 (pure, versioned): tariff_margin_shock, stressed_margin, stressed_runway
        │
        ▼
policy 0.5.0: knockout → bands → uplift → review cap → limit → STRESSED LIMIT
        │
        ▼
portfolio: book exposure by action (share of outstanding limit, ranked by stressed runway)
```

### The macro agent (optional)

There is one small Strands agent, `macro_agent`, alongside the search team in the Graph. It uses
only the tools below and has a small step cap. It finds the actions that apply to the borrower's
HS chapters and source countries, and proposes HS mappings for product types the lookup table does
not cover. It records nothing it computed itself. A proposed mapping is an `hs_mapping` row with
status `proposed`. It counts only once it is in the versioned table or an analyst has confirmed
it.

| Tool | What it does | Limits |
| --- | --- | --- |
| `tariff_actions` | Federal Register search by HS chapter and country, date-bounded | Structured results only; notices are quoted data |
| `duty_rate` | HTS rate for an HS code and country as of a date | Deterministic API read |
| `macro_series` | A FRED/ALFRED or NY Fed series value as known at a date | Vintage-correct |
| `propose_hs_mapping` | Proposes product type → HS chapter | Never counts until confirmed |
| `record_macro_signal` | Stores a source result as a `macro_signal` row | Source results only, never a computed value |

Without the agent, a nightly job pulls the same sources on a fixed list of HS chapters. The agent
adds borrower-specific lookups. It is a nice-to-have, not the backbone.

## Data

Migration `0010_macro_signals.sql`, with RLS on `tenant_id` where the table is tenant data:

| Table | Key columns | Notes |
| --- | --- | --- |
| `macro_signal` | id, source (federal_register, hts, fred, alfred, nyfed, census), source_ref (document number, series id, HTS code+country), kind (tariff_action, duty_rate, price_index, pressure_index), payload jsonb, url, sha256, occurred_at (effective date), observed_at (publication or vintage date) | **Global**, not tenant data: the same tariff applies to everyone. No RLS needed, but it is append-only with an immutability trigger. A revision is a new row with the same `source_ref`. |
| `hs_mapping` | id, tenant_id, borrower_id, product_key, hs_chapter, status (table, proposed, confirmed, rejected), mapping_version, tool_call_id | Tenant data, so it has RLS. Confirm/reject are append-only underwriter actions. |
| `underwriter_action` | new actions `confirm_hs`, `reject_hs` | Existing table. |

`Application` v3 adds `import_share_cogs` (0–1, nullable) and `import_countries` (ISO codes).

**Time rule.** `observed_at` is the date the fact became public: the Federal Register publication
date, or the ALFRED vintage date for a series. It is never the effective date. A backtest of a
March 2025 loan must not see a tariff announced in April 2025, even if it was backdated.

## Metrics: set `ms-4`

`ms-4` = `ms-3` plus the metrics below. Each is a pure function of `(ledger + public signals +
macro signals + confirmed HS mappings) as of a date`.

| Metric | Formula v1 |
| --- | --- |
| `import_share` | `import_share_cogs` from the application (null if not stated) |
| `tariff_rate_change` | COGS-weighted change in duty rate over the borrower's confirmed HS chapters and stated countries: actions observed by as_of and effective within 180 days after it, against the rate a year before as_of |
| `tariff_margin_shock` | `import_share × tariff_rate_change × (1 − gross_margin_est)`: the share of revenue the extra duty eats if none of it is passed on |
| `stock_buffer_months` | `min(days_of_inventory, 180) / 30`: months of sales already bought at the old price |
| `stressed_margin` | `gross_margin_est − tariff_margin_shock` |
| `stressed_runway` | `cash_runway` recomputed with `stressed_margin`, with the first `stock_buffer_months` at the old margin |
| `supply_pressure` | NY Fed GSCPI as known at as_of (context only; no policy rule uses it in v1) |

There is deliberately **no pass-through assumption** in v1. The stress assumes the borrower
absorbs the whole tariff. That is the conservative case, and it is easy to explain. A
pass-through factor by industry is an open question.

A missing input (no import share stated, no confirmed HS chapter) makes the stress metrics
`not_evaluated`. They never default to zero exposure. Not knowing is not the same as safe.

## Checks: set `cs-6`

`cs-6` = `cs-5` plus:

| Check | Rule | Default severity |
| --- | --- | --- |
| `tariff_exposure` | An action observed by as_of covers ≥ 25% of the borrower's COGS-weighted HS chapters, and `stressed_margin` < 0.10 | review |
| `import_share_unstated` | The borrower sells physical goods (inventory or supplier invoices present), but the application gives no import share | info |
| `stated_vs_invoiced_imports` | (later) Supplier invoices show foreign suppliers above the stated import share | review |

## Policy 0.5.0: stressed limit

The new step runs **after** the limit is computed. It can only reduce the limit, never raise it,
and it never changes the band. Healthy-but-exposed is a sizing question, not a quality question.

```yaml
stress:
  # Reduce the limit when the tariff-stressed runway is short. Never raises; never touches the band.
  requires: { import_share: { present: true } }
  rules:
    - when: { stressed_runway: { max: 3 } }
      limit_factor: 0.5
    - when: { stressed_runway: { max: 6 } }
      limit_factor: 0.75
  floor: 0.5              # the stress never cuts the limit by more than half
  not_evaluated: ignore   # a missing stress input leaves the limit unchanged (the info check flags it)
```

The rules-fired list records what happened, for example: "stressed limit 60,000→45,000:
stressed_runway 4.2 ≤ 6 under tariff_rate_change +0.25 on HS 94 (FR doc 2026-12345)". The
`stress` key is optional, so 0.2.x–0.4.0 replay unchanged.

**Why the limit and not the band:** the band says how well run the business is, and a tariff does
not change that. The limit says how much the lender should put at risk, and that is exactly what
exposure changes. This also keeps adverse-action reasons honest: "credit limited due to tariff
exposure on imported goods" is specific and true.

## Portfolio: book exposure

This is the lender-level view. It is likely the most valuable piece for a lender, and it needs
nothing beyond the per-case metrics.

- **By action.** "Section 301 list 4A (FR 2026-12345): 14 borrowers, 31% of outstanding limit, 4
  with stressed runway < 3 months." Each row links to the cases.
- **By HS chapter and country.** Concentration of the book.
- **What-if (analyst only, never policy).** An analyst can enter a hypothetical rate change on a
  chapter and see the stressed metrics recomputed for the book. These scenarios are labelled as
  such, stored separately, and never feed an assessment.

## Backtesting

Tariff actions have publication dates, and ALFRED keeps past vintages of FRED series, so both
backtest honestly through `known_as_of`. The 2018–2019 Section 301 waves and 2025 tariff actions
give real events to test against. Do borrowers with a high `tariff_margin_shock` default more after
an action? The HS mapping for historical borrowers is the hard part. It needs product data from
the lender's file.

## Security and fair lending

- Exposure is measured on **goods** (HS chapter, origin country of goods, import share), never on
  people. No owner nationality, no location-derived feature. Origin country of goods is a trade
  fact about the product, not a proxy for who owns the business. Compliance should still review
  it before any lender pilot.
- Federal Register notices and series metadata are external text. They reach the agent only as
  quoted untrusted data, as all web content does now.
- The stress only reduces a limit and records why. The reasons it did not fire (not evaluated, no
  action covering the chapters) are recorded too.

## Build order

| Step | Work | Size |
| --- | --- | --- |
| 1 | Migration 0010: `macro_signal` (global, append-only) + `hs_mapping` (RLS); Application v3 fields | S |
| 2 | Source adapters with fixture fetchers: Federal Register, HTS, FRED/ALFRED, NY Fed GSCPI | M |
| 3 | HS mapping table v1 (common e-commerce product types → chapters) + analyst confirm/reject | M |
| 4 | `ms-4` metrics and `cs-6` checks | M |
| 5 | Policy schema + evaluator stress step, `default.yaml` 0.5.0, replay tests for 0.2.2–0.4.0 | S |
| 6 | Case page "Systemic exposure" panel; portfolio book-exposure page | M |
| 7 | `macro_agent` in the Strands Graph (optional) | S |
| 8 | Demo fixtures: one dated tariff action on furniture/homeware (HS 94), one exposed borrower, one stocked-up borrower that rides it out | S |
| 9 | Backtest wiring on the 2018–2019 Section 301 waves | M |

**Hackathon slice:** steps 1, 4, 5, 8 and a minimal step 6, all on fixtures. That is one dated
action, a hand-written HS mapping for the demo borrowers, the stress metrics, the stressed-limit
rule, and a one-line book exposure on the portfolio page.

## Open questions

- [ ] **Pass-through.** Should v2 apply an industry pass-through factor (how much of a tariff the
      borrower can pass on in prices), and from what source?
- [ ] **Import share evidence.** Self-reported first. Which data proves it: supplier invoices,
      customs broker statements, or connected accounting data with vendor countries?
- [ ] **HS mapping.** How far does a fixed product-type table go for typical small e-commerce
      catalogs before analysts drown in confirmations?
- [ ] **Shortages beyond tariffs.** Are commodity price indices (flour, lumber, chips) worth a
      second stress route, or is GSCPI context enough for v1?
- [ ] **Source terms.** All sources listed are US government or Federal Reserve publications.
      Confirm rate limits and attribution for the NY Fed index.
- [ ] **Limit vs. pricing.** Should exposure also feed pricing (rate add-on) if the lender prices
      risk, or stay a limit-only lever?
