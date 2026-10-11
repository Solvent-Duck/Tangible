"""Lender management: the dashboard and the (non-production) email outbox."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter

from tangible.onboarding import services
from tangible.workbench.deps import ConnDep, PrincipalDep

router = APIRouter(prefix="/api")


@router.get("/dashboard")
def dashboard(conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.dashboard(conn, p)


@router.get("/outbox")
def outbox(conn: ConnDep, p: PrincipalDep) -> list[dict[str, Any]]:
    return services.list_outbox(conn, p)


@router.get("/outbox/{email_id}")
def email(email_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.get_email(conn, p, str(email_id))
