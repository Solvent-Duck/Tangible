# Deploying Tangible (Railway)

The hosted environment is the Railway project `tangible`, environment `production`. It has three services.

| Service | What | Deploy from |
|---|---|---|
| `workbench` | FastAPI API (root `Dockerfile`), Clerk auth, evidence on volume `/data` | `railway up --service workbench --detach` from the repo root |
| `web` | Next.js app (`web/Dockerfile`, standalone) | `railway up web --path-as-root --service web --detach` |
| `Postgres` | PostgreSQL 18, private networking only | managed |

## Configuration

### `workbench`

| Variable | Value |
|---|---|
| `TANGIBLE_DATABASE_URL` | Owner-role URL on the private network. Migrations need the owner. |
| `TANGIBLE_AUTH_MODE` | `clerk` |
| `TANGIBLE_CLERK_ISSUER` | The instance's Frontend API URL (`https://<slug>.clerk.accounts.dev`) |
| `TANGIBLE_CLERK_AUTHORIZED_PARTIES` | The web origin |
| `TANGIBLE_PUBLIC_URL` | The web origin (invite links point here) |
| `TANGIBLE_MIGRATE_ON_START` | `1` (applies pending migrations at boot) |
| `TANGIBLE_WEB_PROVIDER`, `TANGIBLE_SIGNAL_PROVIDER` | `fixtures` (no live web lookups) |
| `FORWARDED_ALLOW_IPS` | `*` (trust the platform proxy for client IPs; the rate limit uses them) |
| `TANGIBLE_EVIDENCE_DIR` | Set by the image to `/data/evidence` |

### `web`

| Variable | When it's used |
|---|---|
| `NEXT_PUBLIC_AUTH_MODE=clerk` | Build argument |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Build argument |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in`, `NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up` | Build arguments |
| `TANGIBLE_API_URL` | Build argument: the API's public URL. The `/api` rewrite is fixed at build time. |
| `CLERK_SECRET_KEY` | Runtime |
| `PORT=3000` | Runtime |

Set secrets from stdin (`railway variable set KEY --stdin --service web`), never on the command line.

## Getting demo data in

The hosted API has no Bedrock credentials, so live agent runs happen locally. The seeded database is then restored on Railway.

1. **Seed a fresh local database.** Use the `--run-agent` command in [demo-script.md](demo-script.md), plus these flags:
   - `TANGIBLE_DATABASE_URL=.../tangible_demo`
   - `--store data/hosted-evidence`
   - `--base-url <web origin>` and `TANGIBLE_PUBLIC_URL=<web origin>`
   - `--lender-name "<lender>"` (the name invited businesses see)
   - `--link-subject <clerk id>`
   - `--link-analyst <second clerk id>`, if you want the analyst persona hosted

2. **Dump the database and restore it into a new database on Railway.** `railway ssh` passes stdin through, so the dump never touches a public endpoint:

   ```bash
   docker exec tangible-postgres-1 pg_dump -U tangible -Fc --no-owner tangible_demo > data/tangible_demo.dump
   railway ssh --service Postgres -- sh -c 'createdb -U "$PGUSER" tangible_demo'
   railway ssh --service Postgres -- sh -c 'pg_restore -U "$PGUSER" -d tangible_demo --no-owner' < data/tangible_demo.dump
   ```

3. **Upload the raw evidence files**, so Trace to source resolves:

   ```bash
   COPYFILE_DISABLE=1 tar -C data/hosted-evidence -czf - . | railway ssh --service workbench -- sh -c 'mkdir -p /data/evidence && tar -C /data/evidence -xzf -'
   ```

4. **Point the API at the new database:** set `TANGIBLE_DATABASE_URL` to the same URL with the database name changed. Setting it redeploys the API. Earlier databases stay as they were, so going back is a variable change.

## Checks after a deploy

- `curl https://<api>/healthz` returns `{"ok":true}`, and `curl https://<api>/api/invites` returns 401.
- The web origin serves the landing page with `X-Frame-Options: DENY`, and `/sign-in` shows Clerk.
- Sign in, open the dashboard, open a signed case, then use Trace to source.

## Production notes

- **Clerk:** a production instance needs a domain you own (DNS records from the Clerk dashboard). Change the publishable and secret keys, then `TANGIBLE_CLERK_ISSUER` and the authorized parties.
- **Email:** an outbox only. Add a transport (SES or Resend) behind `notify.mail.Mailer` when there's a sending domain.
- **Connectors:** simulated in onboarding. The Plaid and Rutter sandbox connectors run from the CLI (`tangible-pull`).
- **Evidence:** the volume holds the raw files. For production, move to S3 with Object Lock (`TANGIBLE_EVIDENCE_BUCKET`, `TANGIBLE_KMS_KEY`; already supported).
