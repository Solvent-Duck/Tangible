"""tangible-admin: bootstrap staff users (there is no staff sign-up; borrowers join by invite).

    tangible-admin create-tenant --name "Harbor Credit"
    tangible-admin add-user --tenant T --subject user_2abc... --role credit_admin
                            [--email a@b.test] [--name "Ada Reilly"]

With Clerk, the subject is the Clerk user id (`user_...`, shown in the Clerk dashboard or in
`GET /api/me`). Roles are analyst, lender_underwriter or credit_admin.
"""

import argparse
import sys
import uuid

import psycopg

from tangible.config import database_url
from tangible.db.session import admin_connect, tenant_session

STAFF_ROLES = ("analyst", "lender_underwriter", "credit_admin")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tangible-admin")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("create-tenant", help="create a lender tenant and print its id")
    t.add_argument("--name", required=True)
    u = sub.add_parser("add-user", help="give a signed-in identity a staff role in a tenant")
    u.add_argument("--tenant", required=True, help="tenant id")
    u.add_argument("--subject", required=True, help="the identity's subject (Clerk user id)")
    u.add_argument("--role", required=True, choices=STAFF_ROLES)
    u.add_argument("--email", default=None)
    u.add_argument("--name", default=None, help="display name")
    args = p.parse_args(argv)
    url = database_url()

    if args.command == "create-tenant":
        with admin_connect(url) as admin:
            row = admin.execute(
                "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (args.name,)
            ).fetchone()
        assert row
        print(row[0])
        return

    try:
        tenant = str(uuid.UUID(args.tenant))
    except ValueError:
        print("tangible-admin: --tenant must be a tenant id (a UUID)", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        with tenant_session(url, tenant) as conn:
            conn.execute(
                "INSERT INTO app_user (tenant_id, subject, email, display_name, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant, args.subject, args.email, args.name, args.role),
            )
    except psycopg.Error as e:
        print(f"tangible-admin: {type(e).__name__}: {e}".strip(), file=sys.stderr)
        raise SystemExit(1) from None
    print(f"{args.subject} is now {args.role} in tenant {tenant}")


if __name__ == "__main__":
    main()
