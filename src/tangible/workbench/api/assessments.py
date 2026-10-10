"""Assessment routes: read, export, override (lender roles), replay."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from tangible.workbench import services
from tangible.workbench.deps import ConnDep, PrincipalDep, SettingsDep

router = APIRouter(prefix="/api/assessments")


class Override(BaseModel):
    reason: str


@router.get("/{assessment_id}")
def get_assessment(assessment_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.get_assessment(conn, p, str(assessment_id))


@router.get("/{assessment_id}/export")
def export_assessment(assessment_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.export_assessment(conn, p, str(assessment_id))


@router.post("/{assessment_id}/override", status_code=201)
def override(assessment_id: UUID, body: Override, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.override_assessment(conn, p, str(assessment_id), body.reason)


@router.post("/{assessment_id}/replay")
def replay(
    assessment_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    return services.replay_assessment(conn, settings, p, str(assessment_id))
