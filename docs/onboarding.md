# Borrower onboarding and the web app

Status: built on branch `ui-overhaul`. Non-production: email goes to an outbox, and connecting a source is simulated.

## The flow

1. **Invite.** A credit admin composes an invite in the web app (`/app/invites/new`).
   - Standard financials are always requested: bank, accounting, sales and processors, plus bank statements as a fallback.
   - The admin toggles what else they want: inventory, digital presence, reputation and loyalty, longevity and entity, community, and up to five custom requests.
   - Sending creates the `borrower` and the `invite`, renders the email and records it in `outbound_email`.
2. **Accept.** The business opens `/invite/<token>`, sees who is asking and what for, and creates an account.
   - With Clerk this is a real sign-up; in dev mode it is an email-keyed dev identity.
   - Accepting creates an `app_user` with role `borrower`, scoped to that one borrower.
3. **Onboard.** The business works through `/onboarding`:
   - business profile;
   - connect accounts (a consent step, then a `connection` row);
   - documents and links (uploads become write-once evidence);
   - submit.
4. **Track.** The lender's dashboard (`/app/dashboard`) and the invite page show the stage, item coverage and a timeline.
5. **Diligence.** Once the business has submitted, the credit admin opens a case dated now and assigns an analyst. From there the existing flow runs: plan, investigate, review, sign. Lender staff see the case when it is signed.

The stages shown are derived from `invite_event` rows, never stored: `invited → opened → account_created → connecting → submitted → in_diligence → signed`.

## What is real and what is simulated

| Piece | State |
|---|---|
| Invites, tokens, accept, profile, uploads, links, submit, case opening | Real. Stored, append-only, RLS-scoped. |
| Email | Outbox only (`outbound_email`). Set `TANGIBLE_SMTP_URL=smtp://127.0.0.1:1025` to also deliver to a local Mailpit. |
| Connecting a source | Simulated. Consent is recorded as a `connection` row (source, read-only scopes, granted now); no data is pulled. The real connectors (`tangible.connectors`, Plaid and Rutter) can take over from that row once Plaid Link and Rutter Link are wired into the page. |
| Intangibles items (presence, reputation, longevity, community) | Shown to the lender. The profile's identity fields feed the search agents' entity matching; the ms-3 metrics score public signals, and policy 0.4.0 can lift a band one notch at most. Links the business adds are not yet read by the agents. |
| Imported goods | Real. The stated import mix goes into the `application` row and drives the tariff stress (ms-4, policy 0.5.0 stressed limit). |

## Data

Migration `0011_onboarding.sql`:
- **`invite`**: immutable. The raw token is never stored, only its SHA-256. A re-send is a new invite with `replaces_id`.
- **`invite_event`**: append-only.
- **`borrower_profile`**: append-only, including the stated `import_mix`. Each save also appends an `application` ledger row (`source_ref` `onboarding:<borrower>`), so a case sees what the business had stated by its as-of date.
- **`outbound_email`**.
- **`app_user`** gains the `borrower` role and `borrower_id`.
- **Pre-tenant lookups**: `invite_by_token` and `memberships_for_subject` are `SECURITY DEFINER` functions, the only lookups made before a tenant is known.

The request catalog is versioned like the metric sets (`src/tangible/onboarding/catalog.py`, `rc-1`). Each invite stores the version it used.

Borrower accounts are refused every case, finding and assessment route.

## Auth

`TANGIBLE_AUTH_MODE` on the API and `NEXT_PUBLIC_AUTH_MODE` on the web app:

| Mode | API | Web |
|---|---|---|
| dev | `TANGIBLE_DEV_AUTH=1`; trusts `X-Tangible-User` / `X-Tangible-Tenant` | `/sign-in` is a tenant and user picker |
| clerk | Verifies the Clerk session JWT: JWKS, `iss`, `exp`, and `azp` in `TANGIBLE_CLERK_AUTHORIZED_PARTIES`. The tenant comes from the subject's memberships; a client in several tenants sends `X-Tangible-Tenant`. | Clerk `<SignIn>` / `<SignUp>`; the session token is sent as `Authorization: Bearer` |
| oidc | Bearer JWT access tokens only (API clients). | Not supported; use Clerk for browser sign-in. |

Clerk setup:
1. Create a development instance.
2. Set these on the web app:
   - `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
   - `CLERK_SECRET_KEY`
   - `NEXT_PUBLIC_AUTH_MODE=clerk`
3. Set these on the API:
   - `TANGIBLE_AUTH_MODE=clerk`
   - `TANGIBLE_CLERK_ISSUER` (the instance's Frontend API URL)
   - `TANGIBLE_CLERK_AUTHORIZED_PARTIES` (the web origin, e.g. `http://localhost:3000`)
4. Optionally add `"email": "{{user.primary_email_address}}"` to the session token template.

Staff are not self-service. After a staff member signs in once, run:

```
tangible-admin add-user --tenant <id> --subject <clerk user id> --role credit_admin
```

Or seed a demo tenant with `tangible-demo seed --link-subject <clerk user id>`. `GET /api/me` shows your subject.

## Running locally

```
make db-up && make migrate
TANGIBLE_DEV_AUTH=1 TANGIBLE_PORT=8010 uv run tangible-workbench   # or the `api` launch config
TANGIBLE_API_URL=http://127.0.0.1:8010 make web-dev                 # or the `web` launch config
```

Open http://localhost:3000 and sign in as a demo credit admin (run `make demo` first for a tenant). Then:
1. Send an invite.
2. Copy its link from the Outbox.
3. Open the link to act as the business.

## Deploying

- **Two services:** the API (root `Dockerfile`) and the web app (`web/Dockerfile`, Next standalone).
- **Build-time values:** the web image bakes in `TANGIBLE_API_URL` and the `NEXT_PUBLIC_*` values at build time.
- **Invite links:** set `TANGIBLE_PUBLIC_URL` on the API to the web app's URL, so links point at it.
- **Password gate:** the shared-password gate (`TANGIBLE_ACCESS_PASSWORD`) guards the API only. It does not suit the web app, which needs public `/invite/*` pages, so use Clerk for a hosted demo.

## Not done

- **Real connectors:** Plaid Link and Rutter Link, then pulling data.
- **Real email delivery** (SES or Resend).
- **Reading onboarding uploads before diligence.** They are read when the case is re-based, as today.
- **OIDC browser sign-in.** Removed with the old Jinja/htmx workbench; OIDC remains for API clients with bearer tokens.
