"""tangible-sandbox: wire a vendor sandbox account into the local DB so `tangible-pull` can run
against it. Sandbox only: the Plaid path refuses any base URL that is not a sandbox host.

  plaid   create a Plaid Sandbox Item without Link, exchange it for an access token, seed rows
  rutter  seed rows for a Rutter sandbox connection whose access token you copied from the
          dashboard (Connections > ... > Copy Access Token)

Seeding creates a tenant, borrower, connection and connector_account, and prints the shell
lines for the pull. Credentials come from the environment, never arguments:
  PLAID_CLIENT_ID, PLAID_CLIENT_SECRET (the Sandbox secret), PLAID_BASE_URL=https://sandbox.plaid.com
  RUTTER_CLIENT_ID, RUTTER_CLIENT_SECRET, RUTTER_BASE_URL=https://sandbox.rutterapi.com/versioned,
  RUTTER_SANDBOX_TOKEN
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tangible.config import database_url
from tangible.connectors.base import json_object
from tangible.db.repo import create_borrower
from tangible.db.session import admin_connect, tenant_session

PLAID_SANDBOX_URL = "https://sandbox.plaid.com"
# A non-OAuth Plaid test institution (First Platypus Bank, from memory: confirm on Plaid's
# Sandbox institutions page); pass --institution to override.
PLAID_INSTITUTION = "ins_109508"
PLAID_TOKEN_VAR = "PLAID_SANDBOX_TOKEN"
RUTTER_TOKEN_VAR = "RUTTER_SANDBOX_TOKEN"


def _plaid_post(client: httpx.Client, url: str, body: dict[str, Any]) -> dict[str, Any]:
    """One POST, no retries. On failure, surface Plaid's own error fields (they never echo
    credentials), which the shared connector helper deliberately drops."""
    resp = client.post(url, json=body, headers={"Plaid-Version": "2020-09-14"})
    if not resp.is_success:
        try:
            err = resp.json()
        except ValueError:
            err = {}
        detail = " ".join(
            str(err.get(k)) for k in ("error_type", "error_code", "error_message") if err.get(k)
        )
        raise SystemExit(f"POST {httpx.URL(url).path}: HTTP {resp.status_code} {detail}")
    return json_object(resp, f"POST {httpx.URL(url).path}")


def plaid_access_token(client: httpx.Client, institution: str, custom: Path | None) -> str:
    base = os.environ.get("PLAID_BASE_URL", PLAID_SANDBOX_URL).rstrip("/")
    if "sandbox" not in httpx.URL(base).host:
        raise SystemExit(f"refusing to create sandbox items against {base}")
    creds = {
        "client_id": os.environ["PLAID_CLIENT_ID"],
        "secret": os.environ["PLAID_CLIENT_SECRET"],
    }
    options: dict[str, str] = {}
    if custom:  # user_custom: the password is the config JSON as a single string
        options = {
            "override_username": "user_custom",
            "override_password": json.dumps(json.loads(custom.read_text()), separators=(",", ":")),
        }
    created = _plaid_post(
        client,
        base + "/sandbox/public_token/create",
        {
            **creds,
            "institution_id": institution,
            "initial_products": ["transactions"],
            **({"options": options} if options else {}),
        },
    )
    exchanged = _plaid_post(
        client,
        base + "/item/public_token/exchange",
        {**creds, "public_token": created["public_token"]},
    )
    token = str(exchanged["access_token"])
    for _ in range(30):  # a new Item has no transactions until Plaid's first sync finishes
        probe = _plaid_post(client, base + "/transactions/sync", {**creds, "access_token": token})
        if probe.get("transactions_update_status") != "NOT_READY":
            return token
        time.sleep(2)
    raise SystemExit("Plaid Item still NOT_READY after 60s")


def seed(provider: str, secret_ref: str, external_account_id: str | None) -> dict[str, str]:
    now = datetime.now(UTC)
    with admin_connect(database_url()) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (f"sandbox-{provider}",)
        ).fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(database_url(), tenant) as conn:
        borrower = create_borrower(conn, tenant, f"{provider} sandbox borrower")
        crow = conn.execute(
            "INSERT INTO connection (tenant_id, borrower_id, source, granted_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant, borrower, provider, now),
        ).fetchone()
        assert crow
        arow = conn.execute(
            "INSERT INTO connector_account (tenant_id, borrower_id, connection_id, provider, "
            "secret_ref, external_account_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant, borrower, str(crow[0]), provider, secret_ref, external_account_id),
        ).fetchone()
        assert arow
    return {"tenant_id": tenant, "borrower_id": borrower, "account_id": str(arow[0])}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tangible-sandbox")
    sub = p.add_subparsers(dest="provider", required=True)
    plaid = sub.add_parser("plaid")
    plaid.add_argument("--institution", default=PLAID_INSTITUTION)
    plaid.add_argument(
        "--custom", type=Path, help="Plaid user_custom config JSON (accounts and transactions)"
    )
    sub.add_parser("rutter")
    args = p.parse_args(argv)

    out: dict[str, Any]
    if args.provider == "plaid":
        with httpx.Client(timeout=30.0) as client:
            token = plaid_access_token(client, args.institution, args.custom)
        ids = seed("plaid", f"env:{PLAID_TOKEN_VAR}", None)
        # The one place a sandbox token is printed: the user's own terminal, to be exported.
        print(f"export {PLAID_TOKEN_VAR}={token}")
        out, extra = ids, ""
    else:
        if RUTTER_TOKEN_VAR not in os.environ:
            p.error(f"set {RUTTER_TOKEN_VAR} to the connection's access token first")
        ids = seed("rutter", f"env:{RUTTER_TOKEN_VAR}", None)
        out, extra = ids, " --rutter-streams invoices,bills"
    print(json.dumps(out), file=sys.stderr)
    print(
        f"uv run tangible-pull --tenant-id {ids['tenant_id']} "
        f"--account-id {ids['account_id']}{extra}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
