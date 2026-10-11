# Tangible — Intangibles Plan

Oct 10, 2026 · companion to [build-plan.md](build-plan.md) · status: draft for review

## Summary

Some of a small business's strengths never appear in its ledger: a 4.8-star rating built up over
nine years, a shop the neighborhood keeps coming back to, a website and listings that have been
live longer than the loan term. Today none of this can help a borrower. Agent findings can say
`lowers_risk`, but the policy only uses findings to block or cap a band. This plan adds an
**intangibles** pillar. Web search agents gather public signals about the business, deterministic
metrics score them, and policy 0.4.0 can lift the band **one notch at most** when they are strong.
Strong, well-stocked inventory can earn the same lift.

The core split does not change: **the agent investigates, the tools compute, the policy decides.**
The search agents find and match listings. They never produce a score or a number. Every
signal is a stored, hashed, time-stamped row, and every uplift is a policy rule that records the
values it saw.

v1 is **US only**. The UK adapters in `web/sources.py` stay for the current demo set and are not
extended.

## What counts in the borrower's favor

| Area | Signal | Where it comes from | Hard or soft |
| --- | --- | --- | --- |
| Tangible stock | Stock value at cost, aged stock share, days of inventory | Ledger (`inventory_snapshot`, orders), already supported | Hard |
| Loyalty | Repeat customer share | Ledger orders (`repeat_customer_share` v1), already supported | Hard |
| Loyalty | Reviews that mention return visits or years as a customer | Review excerpts, matched by a versioned keyword rule | Soft |
| Reputation | Rating and review count, shrunk toward a prior | Google Places, Yelp | Soft |
| Reputation | Owner response rate to reviews | Google Places, Yelp review excerpts | Soft |
| Longevity | Years trading: earliest of state formation date, domain registration, first listing seen | State SOS / OpenCorporates, RDAP, listings | Medium |
| Presence | Distinct channels confirmed for this business: website, Google, Yelp, linked social profiles | Listings, the borrower's website | Soft |
| Community | Local press or community mentions about this business | Search API results, confirmed by the extractor | Soft |

Loyalty measured from the ledger beats loyalty read from reviews, so the soft version only adds to
the hard one and never replaces it.

The same lookups also produce **negative** signals. These go through ordinary checks and findings,
not the uplift step:

| Check | Rule | Default severity |
| --- | --- | --- |
| `listing_closed` | A matched Google or Yelp listing says the business is permanently closed | hold |
| `entity_status_inactive` | The state registry shows the entity as dissolved, forfeited or inactive | hold |
| `public_bankruptcy` | A CourtListener bankruptcy docket names the business or an owner | hold |
| `review_volume_vs_orders` | Review count is implausibly high for the order count in the ledger (bought reviews) | review |
| `listing_identity_mismatch` | A listing's address or phone contradicts the application | review |

## How it flows

```
application (name, address, phone, website)
        │
        ▼
search agents ──► entity match (tool-scored; analyst confirms when unsure)
 presence │ loyalty │ reputation
        │
        ▼
source tools (API JSON, or page text → tool-less extractor)
        │
        ▼
public_signal rows (append-only, hashed, observed_at)
        │
        ▼
metrics ms-3 (pure, versioned)  ──►  policy 0.4.0: knockout → bands → UPLIFT → review cap → limit
```

### Search agents

There are three sub-agents: **presence**, **loyalty** and **reputation**. Each one is a short,
budgeted run of the existing loop with its own prompt and a web-only toolset. They run after plan
approval and before the main investigation. The main agent sees their signal rows and can still
call the same tools to follow up a lead.

| Tool | What it does | Limits |
| --- | --- | --- |
| `place_search` | Google Places / Yelp search by name and city; returns candidates | Structured API results only |
| `web_search` | General search API; returns titles, snippets and URLs | Snippets are quoted data; URLs are not fetched by default |
| `fetch_page` | Fetches a page | Only hosts on the case allow-list: the borrower's own domain from the application, plus the source list |
| `match_entity` | Scores a candidate against the application's name, address, phone and website | Deterministic and versioned; the agent cannot override the score |
| `record_signal` | Stores a source result as a `public_signal` row, linked to a match | Rejects a candidate whose match is below threshold and not analyst-confirmed |

Like `web_lookup` today, the agents give a source and a query, never a URL. The one exception is
`fetch_page` on the borrower's own domain.

**Entity resolution is the main risk.** A sub-agent that picks the wrong "Joe's Pizza" hands a
stranger's reputation to the borrower. A page can also try to steer the agent toward a
flattering listing. So the agent only proposes a match. `match_entity` scores it
(`entity_match` v1: normalized name, street address, phone, and website domain). A candidate
below 0.9 needs the analyst to confirm it on the case page, and that confirmation is an
append-only underwriter action. Unconfirmed signals never reach the metrics.

### Extraction

Google Places and Yelp return JSON, which a fixed parser reads with no model involved. Web pages
and search snippets go to a tool-less extractor that works like `documents/reader.py`. It gets
untrusted text between markers, uses a strict reply schema, and only copies fields
(`about_business: bool`, `published_on`, `headline`, `outlet`). It produces no sentiment and no
scores. Review excerpts are matched for loyalty phrases by a **versioned keyword rule**
(`loyalty_mentions` v1), not by a model.

## Data

Migration `0008_public_signals.sql`, with RLS on `tenant_id` like every tenant table:

| Table | Key columns | Notes |
| --- | --- | --- |
| `public_signal` | id, tenant_id, borrower_id, source (google_places, yelp, rdap, state_sos, courtlistener, search, website), source_ref (place id, domain, docket), kind, payload jsonb, url, sha256, evidence_pull_id, occurred_at (null if undated), observed_at | Append-only. A re-pull is a new row with the same `source_ref`; `known_as_of` picks the latest one known at a date. |
| `entity_match` | id, tenant_id, case_id, source, source_ref, candidate jsonb, score, matcher_version, tool_call_id, created_at | Written only by `match_entity`. |
| `underwriter_action` | new actions `confirm_match`, `reject_match` | Existing append-only table. |

`Application` needs new fields: `legal_name`, `dba`, `address`, `phone`, `website`, `state`,
`naics`. This is a new ledger row shape, and older rows read these fields as null.

The raw API response for each pull goes to the evidence store, so any metric can be traced back
to the bytes it came from. **Check before building:** the Google Maps Platform and Yelp terms
limit how long their content can be stored. We may only be able to keep place IDs plus our own
derived numbers. That would conflict with immutable evidence, so a lawyer needs to look at it
first (see open questions).

## Metrics: set `ms-3`

`ms-3` = `ms-2` plus the metrics below. Each is a pure function of `(ledger + public signals) as
of a date`, and any formula change gets a new version.

| Metric | Formula v1 |
| --- | --- |
| `review_rating_shrunk` | (C·m + Σ rᵢ·nᵢ) ÷ (C + Σ nᵢ) over confirmed Google + Yelp listings; m = 3.8, C = 25. A few glowing reviews can't beat a long record. |
| `review_count` | Σ nᵢ over confirmed listings |
| `owner_response_rate` | Owner replies ÷ review excerpts seen |
| `years_trading` | (as_of − earliest of formation date, domain registration, first listing review) in years |
| `presence_channels` | Count of distinct confirmed channels |
| `loyalty_mention_share` | Excerpts matching `loyalty_mentions` v1 ÷ excerpts seen |
| `local_press_mentions_3y` | Extractor-confirmed articles about the business, dated in the last 3 years |
| `stock_value_at_cost` | Existing `_stock_value`, exposed as a metric |
| `stock_cover` | `stock_value_at_cost` ÷ the band's limit `max` (or the requested amount, when the application has one) |
| `entity_match_ok` | 1 if every signal used comes from a confirmed match, else 0 |

The ledger metrics `repeat_customer_share`, `aged_stock_share` and `days_of_inventory` are
unchanged and are reused by the uplift rules.

## Policy 0.4.0: uplift step

The new step runs after bands and **before** the review cap, so an open review finding still caps
the band. It never runs after a knockout.

```yaml
uplift:
  # One notch at most, and never above the ceiling. Any one route below is enough.
  max_notches: 1
  ceiling: B
  requires: { entity_match_ok: { min: 1 } }   # applies to the reputation route only
  routes:
    reputation:
      { review_rating_shrunk: { min: 4.4 }, review_count: { min: 75 }, years_trading: { min: 3 } }
    loyalty:
      { repeat_customer_share: { min: 0.35 }, loyalty_mention_share: { min: 0.15 } }
    stock:
      { stock_cover: { min: 1.0 }, aged_stock_share: { max: 0.20 }, days_of_inventory: { max: 120 } }
```

- The rules-fired list says what happened, e.g. "uplift C→B via loyalty: repeat_customer_share
  0.41 ≥ 0.35 …". A route with a missing metric is `not_evaluated` and does not fire.
- `uplift` is an optional key. Policies 0.2.x and 0.3.0 parse and replay exactly as they do now.
- All thresholds above are guesses. See the backtest section.

**Why one notch, a ceiling, and no stacking:** reviews can be bought and presence can be built
quickly. The uplift is sized so that a successfully gamed signal costs one band at most. It also
cannot override a hold or the review cap, and the `review_volume_vs_orders` check is there to catch
the most common way of gaming it.

## Backtesting

Pages fetched today are stamped with today's `observed_at`, so they correctly add nothing to a past
loan. To test the uplift on the lender's history we need dated sources:

- **Wayback Machine** snapshots of the borrower's website and public listing pages dated before
  each decision. Each one becomes a `public_signal` row with `observed_at` set to the snapshot
  time. This is honest because the page was public at that time.
- **RDAP, state formation dates and CourtListener dockets** have their own dates and backtest
  well.
- **Google and Yelp ratings have no history.** The reputation route therefore can't be tested at
  first. Until pilot data comes in, set the default policy's reputation route to analyst-visible
  only (route present but `enabled: false`), and turn it on per lender.

## Security and fair lending

- Review text, pages and snippets are hostile. They reach agents only as quoted untrusted data,
  the extractor has no tools, and add a few poisoned pages (fake 5-star listings, "this is the
  real business" injections) to the injection set. The Brightwell fixture already has one.
- **ECOA / Reg B covers business credit.** "Local" signals must not stand in for where the
  business is located or who owns it. v1 uses no location-derived feature (no neighborhood, ZIP
  or census data), and the loyalty route works from customer behavior only. Compliance should
  review this before any lender pilot.
- The uplift only ever helps the borrower. Even so, the reasons it did *not* fire are recorded,
  because adverse-action reasons may need them.

## Build order

| Step | Work | Size |
| --- | --- | --- |
| 1 | Application v2 fields; migration 0008; `public_signal` ledger type with `known_as_of`; evidence links | S |
| 2 | Source tools with fixture fetchers: Google Places, Yelp, RDAP, CourtListener, search API, own website | M |
| 3 | `match_entity` v1, analyst confirm/reject on the case page | M |
| 4 | Tool-less page extractor + `loyalty_mentions` v1 rule | S |
| 5 | Three sub-agent prompts and a pre-investigation runner reusing the loop with budgets | M |
| 6 | `ms-3` metrics, the five negative checks as `cs-5` | M |
| 7 | Policy schema + evaluator uplift step, `default.yaml` 0.4.0, replay tests for 0.2.2 / 0.3.0 | S |
| 8 | Case page: "Intangibles" panel listing signals, matches, routes passed and failed | S |
| 9 | US demo fixtures (three borrowers: one lifted by reputation, one lifted by stock, one with a bought-review pattern) | S |
| 10 | Wayback adapter and backtest wiring | M |

Steps 1–4 and 7 can run in parallel once the step 1 types are fixed. Everything runs on fixtures
until the vendor and ToS questions are settled.

## Open questions

- [ ] **Vendors and terms:** do Google Places and Yelp allow us to store their content as
      evidence, and for how long? Which search API should we use?
- [ ] **State registries:** use OpenCorporates (paid, one API) or scrape each state's SOS (free,
      50 different formats)?
- [ ] **Social:** profiles linked from the borrower's site count toward presence. Should we also
      offer a consented Instagram or Facebook connection, the same way bank data is connected?
- [ ] **Uplift size:** is one notch with a ceiling of B right for Flex.one's book, or should it
      act on the limit instead of the band?
- [ ] **Demo:** the current five demo borrowers are UK. Should we add US borrowers or move these
      five to the US?
