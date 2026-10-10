"""UI routes: parse forms, call `services`, render templates. No business logic here."""

import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from tangible.agent.llm import ModelConfigError
from tangible.db.session import Conn, tenant_session
from tangible.workbench import services
from tangible.workbench.auth import TENANT_HEADER, USER_HEADER, Principal, Unauthenticated
from tangible.workbench.deps import SettingsDep
from tangible.workbench.ui import queries

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

# htmx is loaded from a CDN, pinned to one version. TODO(production): vendor this file into
# static/ and serve it from our own origin (add SRI or drop the CDN) before any production use.
HTMX_URL = "https://unpkg.com/htmx.org@1.9.12"

COOKIE = "tangible_dev"
FLASH = "tangible_flash"
SEVERITY_ORDER = ("hold", "review", "info")

templates = Jinja2Templates(directory=HERE / "templates")  # autoescape is on for .html
templates.env.globals["HTMX_URL"] = HTMX_URL
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, sort_keys=True, default=str)

router = APIRouter()


def setup(app: FastAPI) -> None:
    app.state.ui_secret = os.environ.get("TANGIBLE_UI_SECRET", "").encode() or secrets.token_bytes(
        32
    )


# ---------------------------------------------------------------- auth (same as the API)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def _make_cookie(secret: bytes, tenant: str, subject: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps([tenant, subject]).encode()).decode()
    return f"{payload}.{_sign(secret, payload)}"


def _read_cookie(secret: bytes, value: str | None) -> tuple[str, str] | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        tenant, subject = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    return str(tenant), str(subject)


def ui_principal(request: Request) -> Principal:
    """The API's authenticator, fed the identity headers or (dev auth only) the signed cookie."""
    settings = request.app.state.settings
    auth = request.app.state.authenticator
    headers: Mapping[str, str] = request.headers
    if TENANT_HEADER not in headers and settings.dev_auth:
        ident = _read_cookie(request.app.state.ui_secret, request.cookies.get(COOKIE))
        if ident:
            headers = {TENANT_HEADER: ident[0], USER_HEADER: ident[1]}
    try:
        p: Principal = auth.authenticate(headers)
        return p
    except (Unauthenticated, NotImplementedError) as e:
        if settings.dev_auth:
            raise HTTPException(303, headers={"Location": "/dev/login"}) from None
        raise HTTPException(401, str(e)) from None


UiPrincipal = Annotated[Principal, Depends(ui_principal)]


def ui_conn(p: UiPrincipal, settings: SettingsDep) -> Iterator[Conn]:
    with tenant_session(settings.database_url, p.tenant_id) as conn:
        yield conn


UiConn = Annotated[Conn, Depends(ui_conn)]


# ---------------------------------------------------------------- rendering helpers

_ERRORS: dict[type[Exception], tuple[int, str]] = {
    services.NotFound: (404, "Not found"),
    services.Forbidden: (403, "Not allowed"),
    services.Conflict: (409, "Can't do that right now"),
    services.Invalid: (422, "Please check your input"),
}


_CAUGHT: tuple[type[Exception], ...] = (*_ERRORS, ModelConfigError)


def _flash_value(kind: str, message: str) -> str:
    raw = json.dumps({"kind": kind, "message": message[:1000]}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _flash_read(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH)
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw))
        return {"kind": str(data["kind"]), "message": str(data["message"])}
    except (ValueError, KeyError, TypeError):
        return None


def render(
    request: Request, name: str, p: Principal | None, status: int = 200, **ctx: Any
) -> HTMLResponse:
    flash = _flash_read(request)
    resp = templates.TemplateResponse(
        request, name, {"p": p, "flash": flash, "has_cookie": COOKIE in request.cookies, **ctx},
        status_code=status,
    )  # fmt: skip
    if flash:
        resp.delete_cookie(FLASH)
    return resp


def redirect(url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie(FLASH, _flash_value(kind, message), httponly=True, samesite="lax")
    return resp


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn domain errors into friendly pages (and roll back anything the request wrote)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except _CAUGHT as e:
            conn = kw.get("conn")
            if conn is not None:
                conn.rollback()
            status, title = _ERRORS.get(type(e), (503, "Model not available"))
            return render(
                kw["request"], "error.html", kw.get("p"), status, title=title, message=str(e)
            )

    return wrapper


def _id(value: UUID) -> str:
    return str(value)


# ---------------------------------------------------------------- dev login


def _dev_only(request: Request) -> None:
    if not request.app.state.settings.dev_auth:
        raise HTTPException(404)


@router.get("/dev/login", response_class=HTMLResponse)
def dev_login(request: Request, settings: SettingsDep, tenant: str = "") -> Response:
    _dev_only(request)
    tenant = tenant.strip()
    people: list[dict[str, str]] | None = None
    error = None
    if tenant:
        try:
            tenant = str(uuid.UUID(tenant))
            with tenant_session(settings.database_url, tenant) as conn:
                people = queries.users(conn)
        except ValueError:
            error = "That is not a valid tenant id (a UUID)."
    return render(request, "login.html", None, tenant=tenant, people=people, error=error)


@router.post("/dev/login")
def dev_login_post(
    request: Request, tenant: Annotated[str, Form()] = "", subject: Annotated[str, Form()] = ""
) -> Response:
    _dev_only(request)
    try:
        p = request.app.state.authenticator.authenticate(
            {TENANT_HEADER: tenant, USER_HEADER: subject}
        )
    except Unauthenticated as e:
        return render(request, "error.html", None, 401, title="Sign-in failed", message=str(e))
    resp = redirect("/", f"Signed in as {p.subject} ({p.role}).")
    resp.set_cookie(
        COOKIE, _make_cookie(request.app.state.ui_secret, p.tenant_id, p.subject),
        httponly=True, samesite="lax",
    )  # fmt: skip
    return resp


@router.post("/dev/logout")
def dev_logout(request: Request) -> Response:
    _dev_only(request)
    resp = redirect("/dev/login")
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- cases


@router.get("/", response_class=HTMLResponse)
@guarded
def home(request: Request, conn: UiConn, p: UiPrincipal) -> Response:
    cases = services.list_cases(conn, p)
    signed = queries.assessment_by_case(conn, [c["id"] for c in cases])
    return render(request, "cases.html", p, cases=cases, signed=signed)


@router.get("/cases/new", response_class=HTMLResponse)
@guarded
def new_case_form(request: Request, conn: UiConn, p: UiPrincipal) -> Response:
    if p.role != "credit_admin":
        raise services.Forbidden("only a credit admin can create cases")
    return render(
        request, "case_new.html", p,
        borrowers=queries.borrowers(conn), analysts=queries.analysts(conn),
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"),
    )  # fmt: skip


@router.post("/cases/new")
@guarded
def new_case(
    request: Request,
    conn: UiConn,
    p: UiPrincipal,
    borrower_id: Annotated[str, Form()] = "",
    as_of: Annotated[str, Form()] = "",
    loan_amount: Annotated[str, Form()] = "",
    budget_steps: Annotated[str, Form()] = "40",
    budget_usd: Annotated[str, Form()] = "5.00",
    assigned_analyst: Annotated[str, Form()] = "",
) -> Response:
    try:
        when = datetime.fromisoformat(as_of)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)  # the form takes UTC
        steps = int(budget_steps)
        usd = Decimal(budget_usd)
        loan = Decimal(loan_amount) if loan_amount.strip() else None
        uuid.UUID(borrower_id)
    except (ValueError, InvalidOperation):
        raise services.Invalid("borrower, as_of, loan amount and budgets must be valid") from None
    case = services.create_new_case(
        conn, p, borrower_id=borrower_id, as_of=when, loan_amount=loan,
        budget_steps=steps, budget_usd=usd, assigned_analyst=assigned_analyst or None,
    )  # fmt: skip
    who = case["assigned_analyst"] or "nobody (assign it below)"
    return redirect("/cases/new", f"Case {case['id']} created, assigned to {who}.")


@router.post("/cases/assign")
@guarded
def assign(
    request: Request,
    conn: UiConn,
    p: UiPrincipal,
    case_id: Annotated[str, Form()] = "",
    analyst: Annotated[str, Form()] = "",
) -> Response:
    try:
        uuid.UUID(case_id.strip())
    except ValueError:
        raise services.Invalid("that is not a case id") from None
    services.assign_analyst(conn, p, case_id.strip(), analyst)
    return redirect("/cases/new", f"Case {case_id.strip()} assigned to {analyst}.")


def _latest_plan(detail: dict[str, Any]) -> dict[str, Any] | None:
    plans = detail.get("plans") or []
    return plans[-1] if plans else None


@router.get("/cases/{case_id}", response_class=HTMLResponse)
@guarded
def case_page(request: Request, case_id: UUID, conn: UiConn, p: UiPrincipal) -> Response:
    d = services.get_case_detail(conn, p, _id(case_id))
    groups = [(sev, [f for f in d["findings"] if f["severity"] == sev]) for sev in SEVERITY_ORDER]
    return render(
        request, "case.html", p, c=d,
        groups=[(s, fs) for s, fs in groups if fs],
        metrics=queries.case_metrics(conn, d["borrower_id"], d["as_of"]),
        late=queries.late_uploads(conn, d["borrower_id"], d["as_of"]),
        plan=_latest_plan(d), is_analyst=p.role == "analyst",
    )  # fmt: skip


@router.get("/cases/{case_id}/status", response_class=HTMLResponse)
@guarded
def case_status(request: Request, case_id: UUID, conn: UiConn, p: UiPrincipal) -> Response:
    # list_cases applies the same role filtering as the case page, without a per-poll audit row.
    row = next((c for c in services.list_cases(conn, p) if c["id"] == _id(case_id)), None)
    if row is None:
        raise services.NotFound(f"case {case_id} not found")
    if not is_htmx(request):
        return redirect(f"/cases/{case_id}")
    resp = render(request, "_status.html", p, c=row)
    if row["status"] != "investigating":
        resp.headers["HX-Refresh"] = "true"  # finished: reload for findings and metrics
    return resp


@router.post("/cases/{case_id}/plan")
@guarded
def propose_plan(
    request: Request, case_id: UUID, conn: UiConn, p: UiPrincipal, settings: SettingsDep
) -> Response:
    plan = services.propose_plan(conn, settings, p, _id(case_id))
    return redirect(f"/cases/{case_id}", f"Plan v{plan['version']} proposed.")


@router.post("/cases/{case_id}/plan/approve")
@guarded
def approve_plan(
    request: Request,
    case_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    plan_id: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    original: Annotated[str, Form()] = "",
) -> Response:
    edited: dict[str, Any] | None = None
    try:
        new = json.loads(body) if body.strip() else None
        old = json.loads(original) if original.strip() else None
    except ValueError:
        raise services.Invalid("the plan must be valid JSON") from None
    if new is not None and new != old:
        if not isinstance(new, dict):
            raise services.Invalid("the plan must be a JSON object")
        edited = new
    res = services.approve_plan(conn, p, _id(case_id), plan_id or None, edited)
    return redirect(
        f"/cases/{case_id}", "Plan edited and approved." if res["edited"] else "Plan approved."
    )


@router.post("/cases/{case_id}/investigate")
@guarded
def investigate(
    request: Request,
    case_id: UUID,
    p: UiPrincipal,
    settings: SettingsDep,
    background: BackgroundTasks,
) -> Response:
    with tenant_session(settings.database_url, p.tenant_id) as conn:  # committed before the job
        job = services.start_investigation(conn, settings, p, _id(case_id))
    background.add_task(job.run)
    return redirect(f"/cases/{case_id}", "Investigation started.")


@router.post("/cases/{case_id}/send-back")
@guarded
def send_back(
    request: Request,
    case_id: UUID,
    p: UiPrincipal,
    settings: SettingsDep,
    background: BackgroundTasks,
    question: Annotated[str, Form()] = "",
) -> Response:
    with tenant_session(settings.database_url, p.tenant_id) as conn:
        job = services.start_send_back(conn, settings, p, _id(case_id), question)
    background.add_task(job.run)
    return redirect(f"/cases/{case_id}", "Sent back to the agent with your question.")


@router.post("/cases/{case_id}/findings/{finding_id}/review")
@guarded
def review(
    request: Request,
    case_id: UUID,
    finding_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    action: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    error = None
    try:
        services.review_finding(conn, p, _id(case_id), _id(finding_id), action, reason)
    except services.Invalid as e:
        if not is_htmx(request):
            raise
        conn.rollback()
        error = str(e)
    if not is_htmx(request):
        return redirect(f"/cases/{case_id}#finding-{finding_id}", "Review recorded.")
    d = services.get_case_detail(conn, p, _id(case_id))
    f = next(x for x in d["findings"] if x["id"] == _id(finding_id))
    return render(
        request, "_finding.html", p, f=f, c=d, is_analyst=True, error=error
    )  # inline swap


@router.post("/cases/{case_id}/questions/{question_id}/approve")
@guarded
def approve_question(
    request: Request, case_id: UUID, question_id: UUID, conn: UiConn, p: UiPrincipal
) -> Response:
    res = services.approve_question(conn, p, _id(case_id), _id(question_id))
    return redirect(f"/cases/{case_id}#questions", res["note"])


@router.post("/cases/{case_id}/documents")
@guarded
def upload(
    request: Request,
    case_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    settings: SettingsDep,
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    if file is None:
        raise services.Invalid("choose a file to upload")
    data = file.file.read(settings.max_upload_bytes + 1)
    res = services.upload_document(conn, settings, p, _id(case_id), file.filename or "", data)
    return redirect(f"/cases/{case_id}#documents", res["note"])


@router.get("/cases/{case_id}/sign", response_class=HTMLResponse)
@guarded
def sign_confirm(request: Request, case_id: UUID, conn: UiConn, p: UiPrincipal) -> Response:
    d = services.get_case_detail(conn, p, _id(case_id))
    if p.role != "analyst":
        raise services.Forbidden("only the assigned analyst can sign")
    if d["status"] != "review":
        raise services.Conflict(f"case is '{d['status']}'; signing needs status 'review'")
    counts: dict[str, int] = {}
    for f in d["findings"]:
        key = f"{f['severity']} / {f['review_state']}"
        counts[key] = counts.get(key, 0) + 1
    return render(request, "sign.html", p, c=d, counts=sorted(counts.items()))


@router.post("/cases/{case_id}/sign")
@guarded
def sign(
    request: Request,
    case_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    settings: SettingsDep,
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if confirm != "yes":
        raise services.Invalid("tick the confirmation box to sign")
    res = services.sign_case(conn, settings, p, _id(case_id))
    return redirect(f"/assessments/{res['assessment_id']}", "Signed. The assessment is frozen.")


@router.post("/cases/{case_id}/rebase")
@guarded
def rebase(request: Request, case_id: UUID, conn: UiConn, p: UiPrincipal) -> Response:
    new = services.rebase_case(conn, p, _id(case_id))
    return redirect(f"/cases/{new['id']}", "Re-based: this is the new case; the old one is closed.")


@router.get("/cases/{case_id}/findings/{finding_id}/trace", response_class=HTMLResponse)
@guarded
def trace(
    request: Request,
    case_id: UUID,
    finding_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    settings: SettingsDep,
) -> Response:
    t = services.trace_finding(conn, settings, p, _id(case_id), _id(finding_id))
    return render(request, "trace.html", p, t=t, case_id=_id(case_id))


# ---------------------------------------------------------------- assessments

_RULE_KEYS = {"rule", "step", "saw", "op", "threshold", "outcome"}


@router.get("/assessments/{assessment_id}", response_class=HTMLResponse)
@guarded
def assessment_page(
    request: Request, assessment_id: UUID, conn: UiConn, p: UiPrincipal
) -> Response:
    a = services.get_assessment(conn, p, _id(assessment_id))
    rules = [
        {**r, "extra": {k: v for k, v in r.items() if k not in _RULE_KEYS}}
        for r in a["rules_fired"]
    ]
    return render(
        request, "assessment.html", p, a=a, rules=rules,
        can_override=p.role in ("lender_underwriter", "credit_admin"),
        can_replay=p.role != "lender_underwriter",
    )  # fmt: skip


@router.get("/assessments/{assessment_id}/export")
@guarded
def export(request: Request, assessment_id: UUID, conn: UiConn, p: UiPrincipal) -> Response:
    out = services.export_assessment(conn, p, _id(assessment_id))
    return JSONResponse(
        out,
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id}.json"'},
    )


@router.post("/assessments/{assessment_id}/override")
@guarded
def override(
    request: Request,
    assessment_id: UUID,
    conn: UiConn,
    p: UiPrincipal,
    reason: Annotated[str, Form()] = "",
) -> Response:
    services.override_assessment(conn, p, _id(assessment_id), reason)
    return redirect(
        f"/assessments/{assessment_id}#overrides", "Override recorded beside the assessment."
    )


@router.post("/assessments/{assessment_id}/replay")
@guarded
def replay(
    request: Request, assessment_id: UUID, conn: UiConn, p: UiPrincipal, settings: SettingsDep
) -> Response:
    res = services.replay_assessment(conn, settings, p, _id(assessment_id))
    if res["match"]:
        return redirect(f"/assessments/{assessment_id}", "Replay matches the frozen assessment.")
    diffs = "; ".join(res["diffs"])
    return redirect(f"/assessments/{assessment_id}", f"Replay DIFFERS: {diffs}", kind="error")
