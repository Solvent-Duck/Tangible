"""Lender invites: compose (with a live preview), send, follow, re-send, revoke, open a case."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from tangible.onboarding import services
from tangible.workbench.deps import ConnDep, PrincipalDep, SettingsDep

router = APIRouter(prefix="/api/invites")


class CustomRequest(BaseModel):
    label: str
    kind: str = "upload"


class Compose(BaseModel):
    email: str = ""
    business_name: str = ""
    contact_name: str | None = None
    loan_amount: Decimal | str | None = None
    message: str | None = None
    items: list[str] = Field(default_factory=list)
    custom: list[CustomRequest] = Field(default_factory=list)
    borrower_id: UUID | None = None

    def body(self) -> dict[str, Any]:
        d = self.model_dump()
        d["borrower_id"] = str(self.borrower_id) if self.borrower_id else None
        return d


class Revoke(BaseModel):
    reason: str = ""


class OpenCase(BaseModel):
    assigned_analyst: str | None = None


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    return services.get_catalog()


@router.post("/preview")
def preview(body: Compose, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.preview_invite_email(conn, p, body.body())


@router.post("", status_code=201)
def create(body: Compose, conn: ConnDep, p: PrincipalDep, settings: SettingsDep) -> dict[str, Any]:
    return services.create_invite(conn, settings, p, body.body())


@router.get("")
def list_invites(conn: ConnDep, p: PrincipalDep) -> list[dict[str, Any]]:
    return services.list_invites(conn, p)


@router.get("/{invite_id}")
def detail(invite_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.invite_detail(conn, p, str(invite_id))


@router.post("/{invite_id}/resend", status_code=201)
def resend(
    invite_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    return services.resend_invite(conn, settings, p, str(invite_id))


@router.post("/{invite_id}/revoke")
def revoke(
    invite_id: UUID, conn: ConnDep, p: PrincipalDep, body: Revoke | None = None
) -> dict[str, Any]:
    return services.revoke_invite(conn, p, str(invite_id), body.reason if body else "")


@router.post("/{invite_id}/open-case", status_code=201)
def open_case(
    invite_id: UUID, conn: ConnDep, p: PrincipalDep, body: OpenCase | None = None
) -> dict[str, Any]:
    return services.open_case(conn, p, str(invite_id), body.assigned_analyst if body else None)
