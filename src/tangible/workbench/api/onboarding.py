"""The borrower's side. `/invite/{token}` is public (the token is the credential); `/accept` needs
only a signed-up identity; everything else needs the borrower user that accepting creates."""

from typing import Any

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel

from tangible.onboarding import services
from tangible.workbench.deps import ConnDep, IdentityDep, PrincipalDep, SettingsDep

router = APIRouter(prefix="/api/onboarding")


class Accept(BaseModel):
    token: str


class Connect(BaseModel):
    provider: str


class Answer(BaseModel):
    value: str


@router.get("/invite/{token}")
def preview(token: str, settings: SettingsDep) -> dict[str, Any]:
    return services.preview_invite(settings.database_url, token)


@router.post("/accept")
def accept(body: Accept, identity: IdentityDep, settings: SettingsDep) -> dict[str, Any]:
    return services.accept_invite(settings.database_url, body.token, identity)


@router.get("/state")
def state(conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.onboarding_state(conn, p)


@router.post("/profile")
def profile(body: dict[str, Any], conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.save_profile(conn, p, body)


@router.post("/items/{item_id}/connect")
def connect(item_id: str, body: Connect, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.connect_source(conn, p, item_id, body.provider)


@router.post("/items/{item_id}/answer")
def answer(item_id: str, body: Answer, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.add_answer(conn, p, item_id, body.value)


@router.post("/items/{item_id}/documents", status_code=201)
def document(
    item_id: str,
    conn: ConnDep,
    p: PrincipalDep,
    settings: SettingsDep,
    file: UploadFile = File(...),  # noqa: B008
) -> dict[str, Any]:
    data = file.file.read(settings.max_upload_bytes + 1)
    return services.add_document(conn, settings, p, item_id, file.filename or "", data)


@router.post("/submit")
def submit(conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.submit(conn, p)
