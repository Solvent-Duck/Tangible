"""Workbench business logic. Route handlers (and the future UI) call these functions only.

Every function takes a tenant-scoped connection (`tenant_session`, so RLS isolates tenants) and
the authenticated `Principal`, and enforces authorization itself. Failures are domain errors
(`NotFound`, `Forbidden`, `Conflict`, `Invalid`) that the API maps to 404/403/409/422.

Return values are JSON-safe dicts (Decimals and datetimes are strings).

Invariants kept here: the agent never sets a number, band or limit (policy does, inside `freeze`);
actions and assessments are append-only; an override sits beside the assessment; the as_of time
rule is never relaxed (late evidence means `rebase`, a new case with a later as_of).
"""

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from tangible.agent.llm import ModelClient
from tangible.agent.loop import approve_plan as _approve_plan
from tangible.agent.loop import investigate as _investigate
from tangible.agent.loop import propose_plan as _propose_plan
from tangible.agent.loop import send_back as _send_back
from tangible.agent.verify import verify_numbers
from tangible.assessment import freeze, record_override, replay
from tangible.assessment.freeze import review_states
from tangible.db.cases import Case, create_case, get_case, set_case_status
from tangible.db.session import Conn, tenant_session
from tangible.documents.upload import store_upload
from tangible.evidence import resolve as evidence_resolve
from tangible.evidence.store import LocalEvidenceStore
from tangible.ledger.evidence import EvidenceRef
from tangible.policy import load_policy
from tangible.web.provider import WebProvider
from tangible.workbench import audit
from tangible.workbench.auth import Principal
from tangible.workbench.settings import Settings

log = logging.getLogger(__name__)

REVIEW_ACTIONS = ("accept", "reject", "dispute", "request_more")
NOT_SENT_NOTE = (
    "Nothing is sent outside Tangible: approving a question records the analyst's approval only."
)
_REF = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}#[^\s\"',\]\)}]+"
)


STUCK_AFTER_ENV = "TANGIBLE_STUCK_AFTER_MIN"
DEFAULT_STUCK_AFTER_MIN = 15.0
# Statuses a run can start from (what `_start` records as prior_status); the reset falls back to
# 'planning' when the started row has none (rows written before prior_status was recorded).
_RESTORABLE = ("open", "planning", "review")
_FALLBACK_STATUS = "planning"
UNPRICED_NOTE = "cost not priced (set TANGIBLE_PRICE_*)"


class WorkbenchError(Exception):
    pass


class NotFound(WorkbenchError):
    pass


class Forbidden(WorkbenchError):
    pass


class Conflict(WorkbenchError):
    pass


class Invalid(WorkbenchError):
    pass


# ---------------------------------------------------------------- helpers


def stuck_after_min() -> float:
    """Minutes after which an 'investigating' case counts as stuck (TANGIBLE_STUCK_AFTER_MIN)."""
    raw = os.environ.get(STUCK_AFTER_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_STUCK_AFTER_MIN
    except ValueError:
        return DEFAULT_STUCK_AFTER_MIN
    return value if value >= 0 else DEFAULT_STUCK_AFTER_MIN


def plain_error(stored: str) -> str:
    """A stored 'ExcType: message' (as written by the failed-investigation audit row) as a plain
    sentence for the analyst. Never raises; unknown errors are shown as stored."""
    text = (stored or "").strip()
    if not text:
        return "The investigation failed for an unknown reason."
    kind, _, msg = text.partition(": ")
    msg = msg.strip().rstrip(".")
    if kind == "ModelConfigError":
        return f"The model is not configured: {msg}."
    lowered = text.lower()
    if "accessdenied" in lowered:
        return (
            "The model provider denied access, so the agent could not run. Check that this "
            f"account has been granted access to the configured model ({msg or text})."
        )
    if "throttling" in lowered or "too many requests" in lowered:
        return (
            "The model provider refused the request as over its rate or quota limit, so the "
            "agent could not run. Check the account's quota for the configured model "
            f"({msg or text})."
        )
    return text if text.endswith((".", "!", "?")) else text + "."


def describe_error(e: BaseException) -> str:
    """`plain_error` for a live exception (e.g. a ModelConfigError raised before a run starts)."""
    return plain_error(f"{type(e).__name__}: {e}")


def _j(v: Any) -> Any:
    """Make a value JSON-safe: Decimal/datetime/UUID to str, recursively."""
    if isinstance(v, dict):
        return {str(k): _j(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_j(x) for x in v]
    if isinstance(v, Decimal | UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _jd(v: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = _j(v)
    return out


def _need_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise Invalid("a reason is required")
    return reason.strip()


def _clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row
    now: datetime = row[0]
    return now


def _load_case(conn: Conn, case_id: str) -> Case:
    try:
        return get_case(conn, case_id)
    except KeyError:
        raise NotFound(f"case {case_id} not found") from None


def _assessment_id_for_case(conn: Conn, case_id: str) -> str | None:
    """The assessment a case was signed into (assessment.case_id, migration 0006)."""
    row = conn.execute("SELECT id FROM assessment WHERE case_id = %s", (case_id,)).fetchone()
    return str(row[0]) if row else None


def _case_for_assessment(conn: Conn, assessment_id: str) -> str | None:
    row = conn.execute("SELECT case_id FROM assessment WHERE id = %s", (assessment_id,)).fetchone()
    return str(row[0]) if row and row[0] else None


def _readable_case(conn: Conn, p: Principal, case_id: str) -> Case:
    """Analysts: their assigned cases. Lender roles: only cases that have been signed."""
    case = _load_case(conn, case_id)
    if p.role == "analyst":
        if case.assigned_analyst != p.subject:
            raise Forbidden("this case is not assigned to you")
    elif _assessment_id_for_case(conn, case.id) is None:
        raise NotFound(f"case {case_id} not found")  # unsigned cases are invisible
    return case


def _driven_case(conn: Conn, p: Principal, case_id: str) -> Case:
    """The assigned analyst, to act on a case (drive the agent, review, sign, ...)."""
    if p.role != "analyst":
        raise Forbidden("only the assigned analyst can do this")
    case = _load_case(conn, case_id)
    if case.assigned_analyst != p.subject:
        raise Forbidden("this case is not assigned to you")
    return case


def _require_status(case: Case, *allowed: str) -> None:
    if case.status not in allowed:
        raise Conflict(
            f"case is '{case.status}'; this needs status {' or '.join(repr(a) for a in allowed)}"
        )


def _case_row(case: Case) -> dict[str, Any]:
    return _jd(
        {
            "id": case.id,
            "borrower_id": case.borrower_id,
            "as_of": case.as_of,
            "loan_amount": case.loan_amount,
            "budget_steps": case.budget_steps,
            "budget_usd": case.budget_usd,
            "assigned_analyst": case.assigned_analyst,
            "status": case.status,
        }
    )


# ---------------------------------------------------------------- cases


def list_cases(conn: Conn, p: Principal) -> list[dict[str, Any]]:
    cols = "c.id, c.borrower_id, b.name, c.as_of, c.status, c.assigned_analyst, c.previous_case_id"
    if p.role == "analyst":
        rows = conn.execute(
            f"SELECT {cols} FROM case_file c JOIN borrower b ON b.id = c.borrower_id "
            "WHERE c.assigned_analyst = %s ORDER BY c.created_at DESC, c.id",
            (p.subject,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {cols} FROM case_file c JOIN borrower b ON b.id = c.borrower_id "
            "WHERE EXISTS (SELECT 1 FROM assessment a WHERE a.case_id = c.id) "
            "ORDER BY c.created_at DESC, c.id"
        ).fetchall()
    keys = (
        "id",
        "borrower_id",
        "borrower",
        "as_of",
        "status",
        "assigned_analyst",
        "previous_case_id",
    )
    return [_j(dict(zip(keys, r, strict=True))) for r in rows]


def create_new_case(
    conn: Conn,
    p: Principal,
    *,
    borrower_id: str,
    as_of: datetime,
    loan_amount: Decimal | None = None,
    budget_steps: int = 40,
    budget_usd: Decimal = Decimal("5.00"),
    assigned_analyst: str | None = None,
) -> dict[str, Any]:
    if p.role != "credit_admin":
        raise Forbidden("only a credit admin can create cases")
    if as_of.tzinfo is None:
        raise Invalid("as_of must include a timezone")
    if budget_steps < 1 or budget_usd <= 0:
        raise Invalid("budgets must be positive")
    if conn.execute("SELECT 1 FROM borrower WHERE id = %s", (borrower_id,)).fetchone() is None:
        raise NotFound(f"borrower {borrower_id} not found")
    if assigned_analyst is not None:
        _require_analyst(conn, assigned_analyst)
    case = create_case(
        conn, p.tenant_id, borrower_id, as_of,
        loan_amount=loan_amount, budget_steps=budget_steps, budget_usd=budget_usd,
        assigned_analyst=assigned_analyst,
    )  # fmt: skip
    audit.record(conn, p, "create", "case", case.id, {"assigned_analyst": assigned_analyst})
    return _case_row(case)


def _require_analyst(conn: Conn, subject: str) -> None:
    row = conn.execute("SELECT role FROM app_user WHERE subject = %s", (subject,)).fetchone()
    if row is None or row[0] != "analyst":
        raise Invalid(f"{subject!r} is not an analyst in this tenant")


def assign_analyst(conn: Conn, p: Principal, case_id: str, analyst_subject: str) -> dict[str, Any]:
    if p.role != "credit_admin":
        raise Forbidden("only a credit admin can assign analysts")
    case = _load_case(conn, case_id)
    _require_status(case, "open", "planning", "investigating", "review")
    _require_analyst(conn, analyst_subject)
    conn.execute(
        "UPDATE case_file SET assigned_analyst = %s WHERE id = %s", (analyst_subject, case.id)
    )
    audit.record(
        conn, p, "assign", "case", case.id,
        {"from": case.assigned_analyst, "to": analyst_subject},
    )  # fmt: skip
    return _case_row(_load_case(conn, case.id))


_FINDING_COLS = (
    "id, check_key, check_version, origin, severity, direction, pillar, summary, evidence_refs, "
    "tool_call_ids, verified, case_id"
)


def _unmatched_numbers(conn: Conn, rows: list[tuple[Any, ...]]) -> dict[str, list[str]]:
    """Display only, never stored: for each unverified model finding, the numbers in its summary
    that the verifier cannot find in the results of the tool calls it cites."""
    todo = [r for r in rows if r[3] == "model" and r[10] is False]
    if not todo:
        return {}
    wanted = sorted({str(i) for r in todo for i in r[9]})
    results = {
        str(i): res
        for i, res in conn.execute(
            "SELECT id, result FROM tool_call WHERE id::text = ANY(%s)", (wanted,)
        ).fetchall()
    }
    return {
        str(r[0]): list(
            verify_numbers(r[7], [results[str(i)] for i in r[9] if str(i) in results]).unmatched
        )
        for r in todo
    }


def _finding_views(
    conn: Conn, where: str, arg: Any, *, with_actions: bool, with_unmatched: bool = False
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_FINDING_COLS} FROM finding WHERE {where} ORDER BY created_at, id", (arg,)
    ).fetchall()
    ids = [str(r[0]) for r in rows]
    states = review_states(conn, ids, _clock(conn))
    acts: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    if with_actions and ids:
        for fid, action, reason, user, at in conn.execute(
            "SELECT finding_id, action, reason, user_id, at FROM underwriter_action "
            "WHERE finding_id = ANY(%s::uuid[]) ORDER BY at, id",
            (ids,),
        ).fetchall():
            acts[str(fid)].append({"action": action, "reason": reason, "by": user, "at": _j(at)})
    unmatched = _unmatched_numbers(conn, rows) if with_unmatched else {}
    out = []
    for r in rows:
        body = r[8] if isinstance(r[8], dict) else {"refs": r[8], "facts": {}}
        out.append(
            {
                "id": str(r[0]),
                "check_key": r[1],
                "check_version": r[2],
                "origin": r[3],
                "severity": r[4],
                "direction": r[5],
                "pillar": r[6],
                "summary": r[7],
                "evidence_refs": list(body.get("refs", [])),
                "facts": dict(body.get("facts", {})),
                "tool_call_ids": list(r[9]),
                "verified": r[10],
                # None: not computed (verified, unchecked, or not a display that needs it)
                "unmatched_numbers": unmatched.get(str(r[0])),
                "case_id": str(r[11]) if r[11] else None,
                "review_state": states[str(r[0])],
                "actions": acts[str(r[0])],
            }
        )
    return out


def get_case_detail(
    conn: Conn, p: Principal, case_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    case = _readable_case(conn, p, case_id)
    out = _case_row(case)
    prev = conn.execute(
        "SELECT previous_case_id FROM case_file WHERE id = %s", (case.id,)
    ).fetchone()
    out["previous_case_id"] = str(prev[0]) if prev and prev[0] else None
    out["findings"] = _finding_views(
        conn, "case_id = %s", case.id, with_actions=True, with_unmatched=True
    )
    aid = _assessment_id_for_case(conn, case.id)
    out["assessments"] = [_assessment_summary(conn, aid)] if aid else []
    if p.role == "analyst":
        out["plans"] = _plans(conn, case.id)
        out["questions"] = _questions(conn, case.id)
        usage = conn.execute(
            "SELECT count(*), COALESCE(sum(cost_usd), 0) FROM model_call WHERE case_id = %s",
            (case.id,),
        ).fetchone()
        assert usage
        # Pricing is configuration (TANGIBLE_PRICE_*, default 0); a $0 total is only meaningful
        # when prices are set, so say so otherwise.
        priced = settings is None or (
            settings.pricing.input_per_mtok > 0 or settings.pricing.output_per_mtok > 0
        )
        out["usage"] = _j(
            {
                "model_calls": usage[0],
                "cost_usd": usage[1],
                "priced": priced,
                "cost_text": UNPRICED_NOTE if usage[0] and not priced else f"${usage[1]}",
            }
        )
        stop = conn.execute(
            "SELECT detail, at FROM audit_log WHERE action = 'investigation_stopped' "
            "AND object_id = %s ORDER BY at DESC LIMIT 1",
            (case.id,),
        ).fetchone()
        out["last_investigation"] = _j({**stop[0], "at": stop[1]}) if stop else None
        out.update(_run_notices(conn, case))
    audit.record(conn, p, "view", "case", case.id)
    return out


def case_status_view(conn: Conn, p: Principal, case_id: str) -> dict[str, Any]:
    """What the polled status partial shows. Same access as the case page, but no audit row per
    poll."""
    case = _readable_case(conn, p, case_id)
    out: dict[str, Any] = {"id": case.id, "status": case.status}
    if p.role == "analyst":
        out.update(_run_notices(conn, case))
    return out


# ---------------------------------------------------------------- run state


def _latest_audit(
    conn: Conn, case_id: str, action: str, *, after: datetime | None = None
) -> tuple[dict[str, Any], datetime, str] | None:
    row = conn.execute(
        "SELECT detail, at, actor FROM audit_log WHERE action = %s AND object_id = %s "
        "AND (%s::timestamptz IS NULL OR at > %s::timestamptz) ORDER BY at DESC, id LIMIT 1",
        (action, case_id, after, after),
    ).fetchone()
    return (dict(row[0]), row[1], row[2]) if row else None


def _run_state(conn: Conn, case: Case) -> dict[str, Any]:
    """The latest investigation start for a case and whether an 'investigating' case is stuck."""
    row = conn.execute(
        "SELECT at, detail, EXTRACT(EPOCH FROM clock_timestamp() - at) / 60.0 FROM audit_log "
        "WHERE action = 'investigation_started' AND object_id = %s ORDER BY at DESC, id LIMIT 1",
        (case.id,),
    ).fetchone()
    limit = stuck_after_min()
    started_at: datetime | None = row[0] if row else None
    age = float(row[2]) if row else None
    prior = row[1].get("prior_status") if row and isinstance(row[1], dict) else None
    # No started row at all means no live job can exist for an 'investigating' case.
    stuck = case.status == "investigating" and (age is None or age >= limit)
    return {
        "started_at": started_at,
        "age_minutes": age,
        "stuck_after_min": limit,
        "stuck": stuck,
        "prior_status": prior if prior in _RESTORABLE else _FALLBACK_STATUS,
    }


def _run_notices(conn: Conn, case: Case) -> dict[str, Any]:
    """The latest failure / reset notice (hidden once a later investigation has started) and, for
    an 'investigating' case, how long it has been running."""
    state = _run_state(conn, case)
    after = state["started_at"]
    failed = _latest_audit(conn, case.id, "investigation_failed", after=after)
    reset = _latest_audit(conn, case.id, "investigation_reset", after=after)
    run = None
    if case.status == "investigating":
        age = state["age_minutes"]
        run = {
            "started_at": state["started_at"],
            "age_minutes": None if age is None else round(age, 1),
            "stuck_after_min": state["stuck_after_min"],
            "stuck": state["stuck"],
            "restores_to": state["prior_status"],
        }
    return {
        "last_failure": None
        if failed is None
        else _j(
            {
                "at": failed[1],
                "error": failed[0].get("error", ""),
                "message": plain_error(str(failed[0].get("error", ""))),
                # rows from before restored_status was recorded: the case's status now
                "restored_status": failed[0].get("restored_status") or case.status,
            }
        ),
        "last_reset": None
        if reset is None
        else _j(
            {
                "at": reset[1],
                "by": reset[2],
                "reason": reset[0].get("reason", ""),
                "restored_status": reset[0].get("restored_status") or case.status,
            }
        ),
        "run": _j(run) if run else None,
    }


def reset_stuck_investigation(
    conn: Conn, p: Principal, case_id: str, reason: str
) -> dict[str, Any]:
    """Put a stuck 'investigating' case back to the status it had before the run. Allowed for
    the assigned analyst and a credit admin, only when the latest start is older than
    TANGIBLE_STUCK_AFTER_MIN. Writes an audit row with the reason; deletes nothing."""
    if p.role == "analyst":
        case = _driven_case(conn, p, case_id)
    elif p.role == "credit_admin":
        case = _load_case(conn, case_id)
    else:
        raise Forbidden("only the assigned analyst or a credit admin can reset an investigation")
    reason = _need_reason(reason)
    conn.execute("SELECT 1 FROM case_file WHERE id = %s FOR UPDATE", (case.id,))
    case = _load_case(conn, case.id)  # re-read under the lock
    if case.status != "investigating":
        raise Conflict(f"case is '{case.status}'; only an 'investigating' case can be reset")
    state = _run_state(conn, case)
    if not state["stuck"]:
        raise Conflict(
            f"the investigation started {state['age_minutes']:.0f} minute(s) ago; it can be "
            f"reset after {state['stuck_after_min']:g} minutes"
        )
    restored = state["prior_status"]
    set_case_status(conn, case.id, restored)
    audit.record(
        conn, p, "investigation_reset", "case", case.id,
        {
            "reason": reason,
            "restored_status": restored,
            "started_at": state["started_at"],
            "stuck_after_min": state["stuck_after_min"],
        },
    )  # fmt: skip
    return {"case_id": case.id, "status": restored, "reset": True}


def _plans(conn: Conn, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, version, body, created_at, EXISTS (SELECT 1 FROM underwriter_action ua "
        "WHERE ua.plan_id = agent_plan.id AND ua.action = 'approve_plan') "
        "FROM agent_plan WHERE case_id = %s ORDER BY version",
        (case_id,),
    ).fetchall()
    return [
        _j({"id": r[0], "version": r[1], "body": r[2], "created_at": r[3], "approved": r[4]})
        for r in rows
    ]


def _questions(conn: Conn, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, body, created_at, EXISTS (SELECT 1 FROM underwriter_action ua "
        "WHERE ua.question_id = borrower_question.id AND ua.action = 'approve_question') "
        "FROM borrower_question WHERE case_id = %s ORDER BY created_at, id",
        (case_id,),
    ).fetchall()
    return [
        _j({"id": r[0], "body": r[1], "created_at": r[2], "approved": r[3], "sent": False})
        for r in rows
    ]


# ---------------------------------------------------------------- plan


def propose_plan(conn: Conn, settings: Settings, p: Principal, case_id: str) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    _require_status(case, "open", "planning")
    res = _propose_plan(
        conn, case, settings.client_factory(),
        policy=load_policy(settings.policy_path), pricing=settings.pricing,
    )  # fmt: skip
    return _jd(
        {"plan_id": res.plan_id, "version": res.version, "body": res.body, "approved": False}
    )


def approve_plan(
    conn: Conn,
    p: Principal,
    case_id: str,
    plan_id: str | None = None,
    edited_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    _require_status(case, "open", "planning")
    if plan_id is None:
        row = conn.execute(
            "SELECT id FROM agent_plan WHERE case_id = %s ORDER BY version DESC LIMIT 1",
            (case.id,),
        ).fetchone()
        if row is None:
            raise Conflict("the case has no plan to approve; propose one first")
        plan_id = str(row[0])
    elif (
        edited_body is None
        and conn.execute(
            "SELECT 1 FROM underwriter_action WHERE plan_id = %s AND action = 'approve_plan'",
            (plan_id,),
        ).fetchone()
    ):
        raise Conflict("that plan is already approved")
    try:
        approved = _approve_plan(conn, case, plan_id, p.subject, edited_body)
    except KeyError:
        raise NotFound(f"plan {plan_id} not found for case {case_id}") from None
    return {"plan_id": approved, "approved": True, "edited": edited_body is not None}


# ---------------------------------------------------------------- investigation


@dataclass
class InvestigationJob:
    """A started investigation. Run it outside the request (`job.run()`, e.g. as a FastAPI
    BackgroundTask); it opens its own tenant_session."""

    settings: Settings
    principal: Principal
    case_id: str
    client: ModelClient
    prior_status: str
    question: str | None = None
    web_provider: WebProvider | None = None  # None: web_lookup reports it is not available

    def run(self) -> None:
        s = self.settings
        try:
            with tenant_session(s.database_url, self.principal.tenant_id) as conn:
                case = get_case(conn, self.case_id)
                policy = load_policy(s.policy_path)
                store = LocalEvidenceStore(s.evidence_dir)
                with tempfile.TemporaryDirectory(prefix="tangible-inv-") as tmp:
                    common: dict[str, Any] = {
                        "pricing": s.pricing,
                        "workdir": Path(tmp),
                        "document_client": self.client,
                        "evidence_store": store,
                        "web_provider": self.web_provider,
                    }
                    if self.question is None:
                        _investigate(conn, case, policy, self.client, **common)
                    else:
                        _send_back(
                            conn, case, policy, self.client, self.question,
                            self.principal.subject, **common,
                        )  # fmt: skip
        except Exception as e:
            log.exception("investigation of case %s failed", self.case_id)
            with tenant_session(s.database_url, self.principal.tenant_id) as conn:
                set_case_status(conn, self.case_id, self.prior_status)
                audit.record(
                    conn, self.principal, "investigation_failed", "case", self.case_id,
                    {
                        "error": f"{type(e).__name__}: {e}"[:500],
                        "restored_status": self.prior_status,
                    },
                )  # fmt: skip


def _start(
    conn: Conn, settings: Settings, p: Principal, case: Case, question: str | None
) -> InvestigationJob:
    client = settings.client_factory()
    job = InvestigationJob(
        settings, p, case.id, client, case.status, question, settings.build_web_provider()
    )
    set_case_status(conn, case.id, "investigating")
    audit.record(
        conn, p, "investigation_started", "case", case.id,
        {"send_back": question is not None, "prior_status": case.status},
    )  # fmt: skip
    return job


def start_investigation(
    conn: Conn, settings: Settings, p: Principal, case_id: str
) -> InvestigationJob:
    """Validate and mark the case 'investigating'; the caller must commit this transaction and
    then run the returned job."""
    case = _driven_case(conn, p, case_id)
    if case.status == "review":
        raise Conflict("the case was already investigated; use send-back to ask more")
    _require_status(case, "open", "planning")
    row = conn.execute(
        "SELECT id FROM agent_plan WHERE case_id = %s ORDER BY version DESC LIMIT 1", (case.id,)
    ).fetchone()
    if row is None:
        raise Conflict("the case has no plan; propose and approve one first")
    ok = conn.execute(
        "SELECT 1 FROM underwriter_action WHERE plan_id = %s AND action = 'approve_plan'",
        (row[0],),
    ).fetchone()
    if ok is None:
        raise Conflict("the latest plan is not approved")
    return _start(conn, settings, p, case, None)


def start_send_back(
    conn: Conn, settings: Settings, p: Principal, case_id: str, question: str
) -> InvestigationJob:
    case = _driven_case(conn, p, case_id)
    _require_status(case, "review")
    return _start(conn, settings, p, case, _need_reason(question))


# ---------------------------------------------------------------- findings review


def review_finding(
    conn: Conn, p: Principal, case_id: str, finding_id: str, action: str, reason: str
) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    if action not in REVIEW_ACTIONS:
        raise Invalid(f"action must be one of {', '.join(REVIEW_ACTIONS)}")
    reason = _need_reason(reason)
    _require_status(case, "review")
    row = conn.execute(
        "SELECT 1 FROM finding WHERE id = %s AND case_id = %s", (finding_id, case.id)
    ).fetchone()
    if row is None:
        raise NotFound(f"finding {finding_id} not found in case {case_id}")
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, finding_id, user_id, action, reason) "
        "VALUES (%s, %s, %s, %s, %s)",
        (case.tenant_id, finding_id, p.subject, action, reason),
    )
    state = review_states(conn, [finding_id], _clock(conn))[finding_id]
    return {"finding_id": finding_id, "action": action, "review_state": state}


# ---------------------------------------------------------------- borrower questions


def list_questions(conn: Conn, p: Principal, case_id: str) -> list[dict[str, Any]]:
    case = _driven_case(conn, p, case_id)
    return _questions(conn, case.id)


def approve_question(conn: Conn, p: Principal, case_id: str, question_id: str) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    row = conn.execute(
        "SELECT 1 FROM borrower_question WHERE id = %s AND case_id = %s", (question_id, case.id)
    ).fetchone()
    if row is None:
        raise NotFound(f"question {question_id} not found in case {case_id}")
    if conn.execute(
        "SELECT 1 FROM underwriter_action WHERE question_id = %s AND action = 'approve_question'",
        (question_id,),
    ).fetchone():
        raise Conflict("that question is already approved")
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, question_id, user_id, action, reason) "
        "VALUES (%s, %s, %s, 'approve_question', 'question approved')",
        (case.tenant_id, question_id, p.subject),
    )
    return {"question_id": question_id, "approved": True, "sent": False, "note": NOT_SENT_NOTE}


# ---------------------------------------------------------------- documents


def upload_document(
    conn: Conn, settings: Settings, p: Principal, case_id: str, filename: str, data: bytes
) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    if case.status == "closed":
        raise Conflict("the case is closed; upload to its successor case")
    if not data:
        raise Invalid("the file is empty")
    if len(data) > settings.max_upload_bytes:
        raise Invalid(f"the file is larger than {settings.max_upload_bytes} bytes")
    pull_id = store_upload(
        conn, LocalEvidenceStore(settings.evidence_dir), case.tenant_id, case.borrower_id,
        filename or "upload", data,
    )  # fmt: skip
    audit.record(
        conn, p, "upload", "evidence_pull", pull_id,
        {"case_id": case.id, "filename": filename, "bytes": len(data)},
    )  # fmt: skip
    # Evidence stored now is observed now; this case's as_of has passed, so it cannot see it.
    rebase_required = datetime.now(UTC) > case.as_of
    return {
        "pull_id": pull_id,
        "byte_size": len(data),
        "rebase_required": rebase_required,
        "note": (
            "The case's as_of is earlier than this upload, so the case cannot use it. "
            "Re-base the case to investigate with it."
            if rebase_required
            else "Stored."
        ),
    }


# ---------------------------------------------------------------- sign, rebase


def sign_case(conn: Conn, settings: Settings, p: Principal, case_id: str) -> dict[str, Any]:
    case = _driven_case(conn, p, case_id)
    if case.status == "signed":
        raise Conflict("the case is already signed")
    _require_status(case, "review")
    rows = conn.execute(
        "SELECT prompt_key, prompt_version, model_id FROM model_call WHERE case_id = %s "
        "ORDER BY at, id",
        (case.id,),
    ).fetchall()
    prompt_versions = {str(k): str(v) for k, v, _ in rows}
    model_ids = [m for _, _, m in rows if m]
    policy = load_policy(settings.policy_path)
    try:
        frozen = freeze(
            conn, case, policy, created_by=p.subject,
            model_id=model_ids[-1] if model_ids else None,
            prompt_versions=prompt_versions,
        )  # fmt: skip
    except ValueError as e:
        raise Conflict(str(e)) from None
    audit.record(
        conn, p, "sign", "assessment", frozen.assessment_id,
        {"case_id": case.id, "band": frozen.result.band},
    )  # fmt: skip
    return _jd(
        {
            "assessment_id": frozen.assessment_id,
            "case_status": "signed",
            "decision_band": frozen.result.band,
            "suggested_limit": frozen.result.limit,
        }
    )


def rebase_case(conn: Conn, p: Principal, case_id: str) -> dict[str, Any]:
    """New case for the same borrower with as_of = now; the old case is closed. The time rule is
    never relaxed: late evidence is only visible to a case whose as_of is after it arrived."""
    old = _driven_case(conn, p, case_id)
    if old.status in ("closed", "investigating"):
        raise Conflict(f"a '{old.status}' case cannot be re-based")
    new = create_case(
        conn, old.tenant_id, old.borrower_id, datetime.now(UTC),
        loan_amount=old.loan_amount, budget_steps=old.budget_steps, budget_usd=old.budget_usd,
        assigned_analyst=old.assigned_analyst,
    )  # fmt: skip
    conn.execute("UPDATE case_file SET previous_case_id = %s WHERE id = %s", (old.id, new.id))
    set_case_status(conn, old.id, "closed")
    audit.record(conn, p, "rebase", "case", new.id, {"previous_case_id": old.id})
    out = _case_row(_load_case(conn, new.id))
    out["previous_case_id"] = old.id
    return out


# ---------------------------------------------------------------- assessments


def _assessment_summary(conn: Conn, assessment_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, borrower_id, as_of, evidence_set_hash, metric_set_version, policy_version, "
        "policy_hash, model_id, prompt_versions, decision_band, suggested_limit, rules_fired, "
        "created_at, created_by FROM assessment WHERE id = %s",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"assessment {assessment_id} not found")
    keys = (
        "id", "borrower_id", "as_of", "evidence_set_hash", "metric_set_version", "policy_version",
        "policy_hash", "model_id", "prompt_versions", "decision_band", "suggested_limit",
        "rules_fired", "created_at", "created_by",
    )  # fmt: skip
    out: dict[str, Any] = _j(dict(zip(keys, row, strict=True)))
    out["case_id"] = _case_for_assessment(conn, assessment_id)
    out["overrides"] = [
        _j({"id": i, "by": u, "reason": r, "at": at})
        for i, u, r, at in conn.execute(
            "SELECT id, user_id, reason, at FROM underwriter_action "
            "WHERE assessment_id = %s AND action = 'override' ORDER BY at, id",
            (assessment_id,),
        ).fetchall()
    ]
    return out


def _readable_assessment(conn: Conn, p: Principal, assessment_id: str) -> None:
    if conn.execute("SELECT 1 FROM assessment WHERE id = %s", (assessment_id,)).fetchone() is None:
        raise NotFound(f"assessment {assessment_id} not found")
    if p.role == "analyst":
        case_id = _case_for_assessment(conn, assessment_id)
        if case_id is None or _load_case(conn, case_id).assigned_analyst != p.subject:
            raise Forbidden("this assessment is not on one of your cases")


def _assessment_full(conn: Conn, assessment_id: str) -> dict[str, Any]:
    out = _assessment_summary(conn, assessment_id)
    out["metrics"] = [
        _j({"key": k, "version": v, "value": val})
        for k, v, val in conn.execute(
            "SELECT mv.metric_key, mv.metric_version, mv.value FROM assessment_item ai "
            "JOIN metric_value mv ON mv.id = ai.ref_id WHERE ai.assessment_id = %s "
            "AND ai.kind = 'metric' ORDER BY mv.metric_key, mv.metric_version",
            (assessment_id,),
        ).fetchall()
    ]
    out["findings"] = _finding_views(
        conn,
        "id IN (SELECT ref_id FROM assessment_item WHERE kind = 'finding' AND assessment_id = %s)",
        assessment_id,
        with_actions=True,
    )
    return out


def get_assessment(conn: Conn, p: Principal, assessment_id: str) -> dict[str, Any]:
    _readable_assessment(conn, p, assessment_id)
    out = _assessment_full(conn, assessment_id)
    audit.record(conn, p, "view", "assessment", assessment_id)
    return out


def export_assessment(conn: Conn, p: Principal, assessment_id: str) -> dict[str, Any]:
    _readable_assessment(conn, p, assessment_id)
    out = _assessment_full(conn, assessment_id)
    audit.record(conn, p, "export", "assessment", assessment_id, {"format": "json"})
    return {"exported_at": datetime.now(UTC).isoformat(), "assessment": out}


def override_assessment(
    conn: Conn, p: Principal, assessment_id: str, reason: str
) -> dict[str, Any]:
    if p.role not in ("lender_underwriter", "credit_admin"):
        raise Forbidden("only lender roles can record overrides")
    reason = _need_reason(reason)
    _readable_assessment(conn, p, assessment_id)
    try:
        action_id = record_override(conn, assessment_id, p.subject, reason)
    except KeyError:
        raise NotFound(f"assessment {assessment_id} not found") from None
    audit.record(conn, p, "override", "assessment", assessment_id, {"action_id": action_id})
    return {"action_id": action_id, "assessment_id": assessment_id}


def replay_assessment(
    conn: Conn, settings: Settings, p: Principal, assessment_id: str
) -> dict[str, Any]:
    if p.role == "lender_underwriter":
        raise Forbidden("lender underwriters cannot replay assessments")
    _readable_assessment(conn, p, assessment_id)
    rep = replay(conn, assessment_id, settings.policy_path)
    audit.record(
        conn, p, "replay", "assessment", assessment_id, {"match": rep.match, "diffs": rep.diffs}
    )
    return {"assessment_id": assessment_id, "match": rep.match, "diffs": rep.diffs}


# ---------------------------------------------------------------- trace


def trace_finding(
    conn: Conn, settings: Settings, p: Principal, case_id: str, finding_id: str
) -> dict[str, Any]:
    """finding -> its tool calls (args, result) -> evidence refs -> the raw line behind each."""
    case = _readable_case(conn, p, case_id)
    findings = [
        f
        for f in _finding_views(
            conn, "case_id = %s", case.id, with_actions=False, with_unmatched=True
        )
        if f["id"] == finding_id
    ]
    if not findings:
        raise NotFound(f"finding {finding_id} not found in case {case_id}")
    f = findings[0]
    calls = [
        _j(
            {
                "id": r[0],
                "tool": r[1],
                "version": r[2],
                "args": r[3],
                "result": r[4],
                "error": r[5],
                "started_at": r[6],
                "duration_ms": r[7],
            }
        )
        for r in conn.execute(
            "SELECT id, tool, tool_version, args, result, error, started_at, duration_ms "
            "FROM tool_call WHERE case_id = %s AND id::text = ANY(%s) ORDER BY started_at, id",
            (case.id, f["tool_call_ids"]),
        ).fetchall()
    ]
    refs: list[str] = list(f["evidence_refs"])
    for c in calls:
        refs += _REF.findall(json.dumps([c["args"], c["result"]], default=str))
    store = LocalEvidenceStore(settings.evidence_dir)
    evidence: list[dict[str, Any]] = []
    for ref in dict.fromkeys(refs):
        item: dict[str, Any] = {"ref": ref, "line": None, "error": None}
        try:
            item["line"] = evidence_resolve.resolve_line(conn, store, EvidenceRef.parse(ref))
        except Exception as e:  # unresolvable or integrity failure: show it, don't hide the trace
            item["error"] = f"{type(e).__name__}: {e}"
        evidence.append(item)
    audit.record(
        conn, p, "view", "finding_trace", finding_id,
        {"case_id": case.id, "refs": len(evidence)},
    )  # fmt: skip
    return {"finding": f, "tool_calls": calls, "evidence": evidence}
