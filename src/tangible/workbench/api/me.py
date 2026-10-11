"""Who am I: the caller's memberships (and, in dev mode, the sign-in picker's data)."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from tangible.db.session import admin_connect, tenant_session
from tangible.workbench.auth import memberships
from tangible.workbench.deps import IdentityDep, SettingsDep

router = APIRouter(prefix="/api")
DEV_TENANT_LIMIT = 30


@router.get("/me")
def me(identity: IdentityDep, settings: SettingsDep) -> dict[str, Any]:
    """Identity-only: works before the caller belongs to any tenant (a borrower who has just
    signed up). The client picks a tenant from `memberships` and sends it as X-Tangible-Tenant."""
    mine = memberships(settings.database_url, identity.subject)
    out: list[dict[str, Any]] = []
    for m in mine:
        with tenant_session(settings.database_url, m.tenant_id) as conn:
            row = conn.execute(
                "SELECT COALESCE(display_name, subject), email, borrower_id FROM app_user "
                "WHERE subject = %s",
                (identity.subject,),
            ).fetchone()
        out.append(
            {
                "tenant_id": m.tenant_id,
                "tenant_name": m.tenant_name,
                "role": m.role,
                "display_name": row[0] if row else identity.subject,
                "email": row[1] if row else identity.email,
                "borrower_id": str(row[2]) if row and row[2] else None,
            }
        )
    return {
        "subject": identity.subject,
        "email": identity.email,
        "auth_mode": settings.auth_mode,
        "memberships": out,
    }


def _dev_only(request: Request) -> None:
    if request.app.state.settings.auth_mode != "dev":
        raise HTTPException(404, "Not Found")


@router.get("/dev/tenants", include_in_schema=False)
def dev_tenants(request: Request, settings: SettingsDep) -> list[dict[str, Any]]:
    """The dev sign-in picker: tenants and their users. A deliberate cross-tenant read through the
    admin connection, reachable only in dev mode (which already lets anyone act as anyone); a
    404 otherwise. Demo tenants first, then newest."""
    _dev_only(request)
    with admin_connect(settings.database_url) as admin:
        tenants = admin.execute(
            "SELECT t.id, t.name, t.created_at FROM tenant t "
            "ORDER BY (t.name LIKE 'demo-%%') DESC, t.created_at DESC LIMIT %s",
            (DEV_TENANT_LIMIT,),
        ).fetchall()
        users = admin.execute(
            "SELECT tenant_id, subject, COALESCE(display_name, subject), role FROM app_user "
            "WHERE tenant_id = ANY(%s::uuid[]) ORDER BY role, subject",
            ([str(t[0]) for t in tenants],),
        ).fetchall()
    by_tenant: dict[str, list[dict[str, str]]] = {}
    for tid, subject, name, role in users:
        by_tenant.setdefault(str(tid), []).append({"subject": subject, "name": name, "role": role})
    return [
        {"id": str(i), "name": n, "created_at": at.isoformat(), "users": by_tenant.get(str(i), [])}
        for i, n, at in tenants
    ]
