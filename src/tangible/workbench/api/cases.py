"""Case-scoped routes. Handlers only parse input and call services."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from tangible.agent.llm import ModelConfigError
from tangible.db.session import tenant_session
from tangible.workbench import display, queries, services
from tangible.workbench.deps import ConnDep, PrincipalDep, SettingsDep

router = APIRouter(prefix="/api/cases")


class CreateCase(BaseModel):
    borrower_id: UUID
    as_of: datetime
    loan_amount: Decimal | None = None
    budget_steps: int = 40
    budget_usd: Decimal = Decimal("5.00")
    assigned_analyst: str | None = None


class Assign(BaseModel):
    analyst: str


class ApprovePlan(BaseModel):
    plan_id: UUID | None = None
    edited_body: dict[str, Any] | None = None


class Review(BaseModel):
    action: str
    reason: str


class SendBack(BaseModel):
    question: str


class ResetInvestigation(BaseModel):
    reason: str


@router.get("")
def list_cases(conn: ConnDep, p: PrincipalDep) -> list[dict[str, Any]]:
    cases = services.list_cases(conn, p)
    signed = queries.assessment_by_case(conn, [c["id"] for c in cases])
    return [{**c, "assessment": signed.get(c["id"])} for c in cases]


@router.post("", status_code=201)
def create_case(body: CreateCase, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.create_new_case(
        conn, p,
        borrower_id=str(body.borrower_id), as_of=body.as_of, loan_amount=body.loan_amount,
        budget_steps=body.budget_steps, budget_usd=body.budget_usd,
        assigned_analyst=body.assigned_analyst,
    )  # fmt: skip


@router.get("/lookups")
def lookups(conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    """Borrowers and analysts, for the new-case form (credit admin only)."""
    if p.role != "credit_admin":
        raise services.Forbidden("only a credit admin can create cases")
    return {"borrowers": queries.borrowers(conn), "analysts": queries.analysts(conn)}


@router.get("/{case_id}")
def get_case(
    case_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    return services.get_case_detail(conn, p, str(case_id), settings)


@router.get("/{case_id}/workspace")
def workspace(
    case_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    """Everything the case page shows in one read: the case detail plus the borrower's name,
    the baseline metrics (with readable labels) and, for the analyst, the documents."""
    d = services.get_case_detail(conn, p, str(case_id), settings)
    docs = services.case_documents(conn, p, str(case_id)) if p.role == "analyst" else []
    d["borrower"] = queries.borrower_name(conn, d["borrower_id"])
    d["metrics"] = [
        {
            **m,
            "label": display.metric_label(m["key"]),
            "text": display.metric_text(m["value"], m["key"]),
        }
        for m in queries.case_metrics(conn, d["borrower_id"], d["as_of"])
    ]
    d["documents"] = docs
    d["late_uploads"] = sum(1 for x in docs if not x["included"])
    d["viewer_role"] = p.role
    return d


@router.get("/{case_id}/status")
def status(case_id: UUID, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    """The live run state, for polling (no audit row per poll)."""
    return services.case_status_view(conn, p, str(case_id))


@router.get("/{case_id}/documents")
def documents(case_id: UUID, conn: ConnDep, p: PrincipalDep) -> list[dict[str, Any]]:
    return services.case_documents(conn, p, str(case_id))


@router.post("/{case_id}/assign")
def assign(case_id: UUID, body: Assign, conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    return services.assign_analyst(conn, p, str(case_id), body.analyst)


@router.post("/{case_id}/plan", status_code=201)
def propose_plan(
    case_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    try:
        return services.propose_plan(conn, settings, p, str(case_id))
    except ModelConfigError as e:
        raise HTTPException(503, services.describe_error(e)) from None


@router.post("/{case_id}/plan/approve")
def approve_plan(
    case_id: UUID, conn: ConnDep, p: PrincipalDep, body: ApprovePlan | None = None
) -> dict[str, Any]:
    body = body or ApprovePlan()
    plan_id = str(body.plan_id) if body.plan_id else None
    return services.approve_plan(conn, p, str(case_id), plan_id, body.edited_body)


@router.post("/{case_id}/investigate", status_code=202)
def investigate(
    case_id: UUID, p: PrincipalDep, settings: SettingsDep, background: BackgroundTasks
) -> dict[str, Any]:
    # Own transaction, committed before the background job starts (it opens its own session).
    try:
        with tenant_session(settings.database_url, p.tenant_id) as conn:
            job = services.start_investigation(conn, settings, p, str(case_id))
    except ModelConfigError as e:
        raise HTTPException(503, services.describe_error(e)) from None
    background.add_task(job.run)
    return {"case_id": str(case_id), "status": "investigating"}


@router.post("/{case_id}/send-back", status_code=202)
def send_back(
    case_id: UUID,
    body: SendBack,
    p: PrincipalDep,
    settings: SettingsDep,
    background: BackgroundTasks,
) -> dict[str, Any]:
    try:
        with tenant_session(settings.database_url, p.tenant_id) as conn:
            job = services.start_send_back(conn, settings, p, str(case_id), body.question)
    except ModelConfigError as e:
        raise HTTPException(503, services.describe_error(e)) from None
    background.add_task(job.run)
    return {"case_id": str(case_id), "status": "investigating"}


@router.post("/{case_id}/reset-investigation")
def reset_investigation(
    case_id: UUID, body: ResetInvestigation, conn: ConnDep, p: PrincipalDep
) -> dict[str, Any]:
    """Un-stick an 'investigating' case whose run was lost (e.g. a server restart)."""
    return services.reset_stuck_investigation(conn, p, str(case_id), body.reason)


@router.post("/{case_id}/findings/{finding_id}/review")
def review_finding(
    case_id: UUID, finding_id: UUID, body: Review, conn: ConnDep, p: PrincipalDep
) -> dict[str, Any]:
    return services.review_finding(conn, p, str(case_id), str(finding_id), body.action, body.reason)


@router.get("/{case_id}/findings/{finding_id}/trace")
def trace(
    case_id: UUID, finding_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep
) -> dict[str, Any]:
    return services.trace_finding(conn, settings, p, str(case_id), str(finding_id))


@router.get("/{case_id}/questions")
def list_questions(case_id: UUID, conn: ConnDep, p: PrincipalDep) -> list[dict[str, Any]]:
    return services.list_questions(conn, p, str(case_id))


@router.post("/{case_id}/questions/{question_id}/approve")
def approve_question(
    case_id: UUID, question_id: UUID, conn: ConnDep, p: PrincipalDep
) -> dict[str, Any]:
    return services.approve_question(conn, p, str(case_id), str(question_id))


@router.post("/{case_id}/documents", status_code=201)
def upload_document(
    case_id: UUID,
    conn: ConnDep,
    p: PrincipalDep,
    settings: SettingsDep,
    file: UploadFile = File(...),  # noqa: B008
    rebase: bool = False,
) -> dict[str, Any]:
    data = file.file.read(settings.max_upload_bytes + 1)
    if rebase:
        # One step for an 'open' or 'planning' case: store the file, then re-base so the new case
        # (dated now) sees it. Returns the new case with the upload under "upload".
        return services.upload_and_rebase(
            conn, settings, p, str(case_id), file.filename or "", data
        )
    return services.upload_document(conn, settings, p, str(case_id), file.filename or "", data)


@router.post("/{case_id}/sign")
def sign(case_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep) -> dict[str, Any]:
    return services.sign_case(conn, settings, p, str(case_id))


@router.post("/{case_id}/rebase", status_code=201)
def rebase(case_id: UUID, conn: ConnDep, p: PrincipalDep, settings: SettingsDep) -> dict[str, Any]:
    return services.rebase_case(conn, p, str(case_id), settings)
