"""Assessment routes: read, export, override (lender roles), replay."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from tangible.workbench import display, queries, services
from tangible.workbench.deps import ConnDep, PrincipalDep, SettingsDep

router = APIRouter(prefix="/api/assessments")


class Override(BaseModel):
    reason: str


@router.get("/{assessment_id}")
def get_assessment(assessment_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    """The frozen assessment, plus display-only extras (borrower name, readable metric labels and
    values). The export below is the record and carries none of these."""
    a = services.get_assessment(conn, p, str(assessment_id))
    a["borrower"] = queries.borrower_name(conn, a["borrower_id"])
    a["metrics"] = [
        {
            **m,
            "label": display.metric_label(m["key"]),
            "text": display.metric_text(m["value"], m["key"]),
        }
        for m in a["metrics"]
    ]
    a["can_override"] = p.role in ("lender_underwriter", "credit_admin")
    a["can_replay"] = p.role != "lender_underwriter"
    return a


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
