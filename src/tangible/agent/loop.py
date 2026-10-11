"""The investigation loop: plan, analyst approval, then a budgeted tool-using model run.

The agent investigates, tools compute, policy decides. Nothing the model writes is executed or
trusted: its tool calls go through `call_tool` (validated, logged), and every tool result goes
back wrapped as quoted, untrusted data. Budgets are enforced here, not by the model.
"""

import html
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tangible.agent.llm import LoggedClient, ModelClient, Pricing
from tangible.agent.prompts import Prompt, load_prompt
from tangible.agent.snapshot import schema_summary
from tangible.agent.tools import (
    ToolContext,
    ToolResult,
    call_tool,
    open_context,
    tool_definitions,
)
from tangible.agent.tools.findings import verification_feedback
from tangible.agent.tools.library import library_catalog
from tangible.agent.tools.runtime import Verifier
from tangible.agent.verify import verify_numbers
from tangible.assessment import run_baseline
from tangible.assessment.baseline import refresh_baseline
from tangible.assessment.hashing import ledger_input_hash
from tangible.db.cases import Case, set_case_status
from tangible.db.findings import load_findings
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.policy import Policy

PROMPT_VERSIONS = {"system": "v1", "investigate": "v4", "plan": "v3"}

# The closing message is stored on the investigation_stopped audit row, capped at this length.
WRAP_UP_MAX_CHARS = 4000

# After this many consecutive steps whose tool calls all failed, the loop asks for a wrap-up.
STALL_STEPS = 3
WRAP_UP_NOTICE = (
    "Notice from the investigation loop (trusted): {why} Stop calling tools now and reply with "
    "your plain-text wrap-up: what you covered, what you found, and what could not be "
    "established."
)


class PlanNotApproved(Exception):
    """investigate() was called without an analyst-approved plan."""


@dataclass(frozen=True)
class PlanResult:
    plan_id: str
    version: int
    body: dict[str, Any]


@dataclass(frozen=True)
class InvestigationResult:
    stop_reason: str  # end_turn | step_budget | cost_budget
    steps: int
    cost_usd: Decimal
    finding_ids: list[str]
    unverified_finding_ids: list[str]
    prompt_versions: dict[str, str]
    model_id: str


def default_verifier(text: str, results: list[object]) -> bool:
    return verify_numbers(text, results).ok


# ---------------------------------------------------------------- plan

_PROFILE_DATES: dict[str, Callable[[Any], date | datetime | None]] = {
    "orders": lambda r: r.occurred_at,
    "refunds": lambda r: r.occurred_at,
    "payouts": lambda r: r.settled_on or r.expected_on,
    "bank_txns": lambda r: r.posted_on,
    "invoices": lambda r: r.issued_on,
    "inventory_snapshots": lambda r: r.as_of,
    "account_balances": lambda r: r.as_of,
    "connections": lambda r: r.granted_at,
    "applications": lambda r: r.observed_at,
    "statements": lambda r: r.period_end,
}


def _day(d: date | datetime) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def borrower_profile(conn: Conn, case: Case) -> dict[str, Any]:
    """Row counts, date ranges and sources per ledger table, as known at the case's as_of."""
    known = load_ledger(conn, case.borrower_id).known(case.as_of)
    tables: dict[str, Any] = {}
    for name, when in _PROFILE_DATES.items():
        rows = getattr(known, name)
        days = [_day(d) for r in rows if (d := when(r)) is not None]
        tables[name] = {
            "rows": len(rows),
            "first": min(days) if days else None,
            "last": max(days) if days else None,
        }
    return {
        "as_of": case.as_of.isoformat(),
        "tables": tables,
        "sources": sorted({c.source for c in known.connections}),
        "has_application": known.application is not None,
    }


def _parse_plan(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    try:
        body = json.loads(t)
        if not isinstance(body, dict):
            raise ValueError("plan must be a JSON object")
        risks = body.get("risks")
        if not isinstance(risks, list) or not all(isinstance(r, dict) for r in risks):
            raise ValueError("plan.risks must be a list of objects")
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        return {"raw": text, "parse_error": str(e)}
    return body


def _insert_plan(conn: Conn, case: Case, body: dict[str, Any]) -> tuple[str, int]:
    row = conn.execute(
        "INSERT INTO agent_plan (tenant_id, case_id, version, body) "
        "VALUES (%s, %s, COALESCE((SELECT max(version) FROM agent_plan WHERE case_id = %s), 0) + 1,"
        " %s::jsonb) RETURNING id, version",
        (case.tenant_id, case.id, case.id, json.dumps(body)),
    ).fetchone()
    assert row
    return str(row[0]), int(row[1])


def _system_text(*names: str) -> tuple[str, list[Prompt]]:
    prompts = [load_prompt(n, int(PROMPT_VERSIONS[n].removeprefix("v"))) for n in names]
    return "\n\n".join(p.text for p in prompts), prompts


def _catalog(policy: Policy) -> dict[str, Any]:
    if policy.parsed is None:
        raise ValueError("policy has no parsed spec")
    return library_catalog(policy.parsed.metric_set, policy.parsed.check_set)


def propose_plan(
    conn: Conn, case: Case, client: ModelClient, *, policy: Policy, pricing: Pricing
) -> PlanResult:
    system, prompts = _system_text("system", "plan")
    logged = LoggedClient(client, conn, case, prompts[-1], pricing)
    profile = json.dumps(borrower_profile(conn, case), indent=2)
    library = json.dumps(_catalog(policy), indent=2)
    content = (
        f"Borrower profile (tool-computed):\n{profile}\n\n"
        f"Library released for this case (checks and metrics you may plan with):\n{library}"
    )
    resp = logged.complete(
        system=system, messages=[{"role": "user", "content": content}], tools=None
    )
    body = _parse_plan(resp.text)
    plan_id, version = _insert_plan(conn, case, body)
    set_case_status(conn, case.id, "planning")
    return PlanResult(plan_id, version, body)


def approve_plan(
    conn: Conn,
    case: Case,
    plan_id: str,
    user_id: str,
    edited_body: dict[str, Any] | None = None,
) -> str:
    row = conn.execute(
        "SELECT 1 FROM agent_plan WHERE id = %s AND case_id = %s", (plan_id, case.id)
    ).fetchone()
    if row is None:
        raise KeyError(f"plan {plan_id} not found for case {case.id}")
    reason = "plan approved"
    if edited_body is not None:
        plan_id, _ = _insert_plan(conn, case, edited_body)
        reason = "plan edited and approved"
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, plan_id, user_id, action, reason) "
        "VALUES (%s, %s, %s, 'approve_plan', %s)",
        (case.tenant_id, plan_id, user_id, reason),
    )
    return plan_id


def _approved_plan(conn: Conn, case: Case) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, body FROM agent_plan WHERE case_id = %s ORDER BY version DESC LIMIT 1",
        (case.id,),
    ).fetchone()
    if row is None:
        raise PlanNotApproved(f"case {case.id} has no plan")
    ok = conn.execute(
        "SELECT 1 FROM underwriter_action WHERE plan_id = %s AND action = 'approve_plan'",
        (row[0],),
    ).fetchone()
    if ok is None:
        raise PlanNotApproved(f"the latest plan for case {case.id} is not approved")
    body: dict[str, Any] = row[1]
    return body


# ---------------------------------------------------------------- investigate


def wrap_tool_output(call_id: str, tool: str, payload: str) -> str:
    # "<" is escaped so tool output can never close the tag early; JSON stays valid.
    safe = payload.replace("<", "\\u003c")
    cid, name = html.escape(call_id, quote=True), html.escape(tool, quote=True)
    return f'<tool_output call_id="{cid}" tool="{name}" untrusted="true">{safe}</tool_output>'


def _baseline_summary(conn: Conn, case: Case, metric_ids: list[str]) -> str:
    metrics = conn.execute(
        "SELECT metric_key, metric_version, value FROM metric_value WHERE id = ANY(%s::uuid[]) "
        "ORDER BY metric_key, metric_version",
        (metric_ids,),
    ).fetchall()
    findings = load_findings(conn, case_id=case.id, current_only=True)
    lines = ["Baseline metrics (tool-computed):"]
    lines += [f"- {k} v{v}: {val if val is not None else 'n/a'}" for k, v, val in metrics]
    lines.append("Baseline rule findings:")
    rules = [f for f in findings if f.origin == "rule"]
    lines += [f"- [{f.draft.severity}] {f.draft.check_key}: {f.draft.summary}" for f in rules]
    if not rules:
        lines.append("- none")
    leads = [f.draft.check_key for f in rules if f.draft.severity in ("review", "hold")]
    lines.append("Leads to follow up (rule findings at review or hold):")
    lines += [f"- {k}" for k in dict.fromkeys(leads)] or ["- none"]
    prior = [f for f in findings if f.origin == "model"]
    if prior:
        lines.append(
            "Findings already recorded by earlier agent runs (current ones only; use the id as "
            "`replaces` to correct one):"
        )
        lines += [
            f"- [{f.draft.severity}] {f.draft.check_key} (id {f.id}): {f.draft.summary} "
            f"(verified={f.verified})"
            for f in prior
        ]
    return "\n".join(lines)


def _borrower_name(conn: Conn, case: Case) -> str | None:
    row = conn.execute("SELECT name FROM borrower WHERE id = %s", (case.borrower_id,)).fetchone()
    return str(row[0]) if row else None


def _first_message(
    plan: dict[str, Any],
    baseline: str,
    reference: dict[str, Any],
    question: str | None,
    borrower_name: str | None = None,
) -> str:
    parts = [
        "Approved investigation plan:", json.dumps(plan, indent=2), "", baseline, "",
        *(
            [f"Borrower name on file (a label to search public records with): {borrower_name}", ""]
            if borrower_name
            else []
        ),
        "Reference (tool-computed): the library released for this case and the ledger "
        "snapshot's tables and columns.",
        json.dumps(reference, indent=2),
    ]  # fmt: skip
    if question:
        parts += [
            "",
            "Question from the analyst (trusted instruction from the underwriter):",
            question,
        ]
    return "\n".join(parts)


@dataclass
class RunSetup:
    """Everything an engine needs for one investigation run. Built once, engine-neutral."""

    case: Case
    ctx: ToolContext
    logged: LoggedClient
    system: str
    tools: list[dict[str, Any]]
    first_message: str
    # Who the business is (trusted, from the case). Set when public-records search is available;
    # the Strands engine then runs the search team before the lead investigator.
    search_brief: str = ""


@dataclass(frozen=True)
class RunOutcome:
    stop_reason: str  # end_turn | step_budget | cost_budget
    steps: int
    model_id: str
    wrap_up: str = ""  # the model's closing message, kept for the analyst (unverified prose)
    detail: dict[str, Any] = field(default_factory=dict)  # engine extras for the audit row


ENGINES = ("strands", "classic")


def engine_from_env() -> str:
    """TANGIBLE_AGENT_ENGINE = strands (default) | classic."""
    name = os.environ.get("TANGIBLE_AGENT_ENGINE", "").strip().lower() or "strands"
    if name not in ENGINES:
        raise ValueError(f"unknown TANGIBLE_AGENT_ENGINE {name!r} (use strands or classic)")
    return name


def search_brief(ctx: ToolContext, borrower_name: str | None) -> str:
    """The public-records search task: who the business is, from the application (trusted)."""
    app = ctx.known.application
    fields = [
        ("Legal name", app.legal_name if app else None),
        ("Trading as", app.dba if app else None),
        ("Address", ", ".join(x for x in (app.address, app.state) if x) if app else None),
        ("Phone", app.phone if app else None),
        ("Website", app.website if app else None),
        ("NAICS industry code", app.naics if app else None),
    ]
    lines = [f"- {k}: {v}" for k, v in fields if v]
    if not lines and borrower_name:
        lines = [f"- Name on file: {borrower_name}"]
    return "\n".join(
        [
            "Business to look up (trusted, from the loan application on file):",
            *(lines or ["- (no identity on file)"]),
            f"Case date: {ctx.case.as_of.date().isoformat()}.",
            "Search the public sources for this business and record what you can confirm.",
        ]
    )


def wrap_up_notice(case: Case, steps: int, stalled: int, notified: set[str]) -> str | None:
    """The trusted wrap-up nudge, at most once per kind. Shared by both engines."""
    why = None
    if case.budget_steps - steps == 1 and "budget" not in notified:
        why, kind = "one model step is left in this investigation's budget.", "budget"
    elif stalled >= STALL_STEPS and "stall" not in notified:
        why, kind = f"the last {stalled} steps returned only tool errors.", "stall"
    if why is None:
        return None
    notified.add(kind)
    return WRAP_UP_NOTICE.format(why=why)


def tool_payload(ctx: ToolContext, name: str, out: ToolResult) -> tuple[str, bool]:
    """What the model sees for one tool call (before wrapping), and whether it failed."""
    if out.error is not None:
        return out.error, True
    shown = out.result
    if name == "record_finding" and isinstance(shown, dict):
        # Feedback for the model only; the logged tool_call result is unchanged.
        shown = {**shown, "status": verification_feedback(ctx, shown)}
    return json.dumps(shown, ensure_ascii=False), False


def run_classic(setup: RunSetup) -> RunOutcome:
    """The hand-written loop: kept as the fallback engine and the parity oracle in tests."""
    case, ctx, logged = setup.case, setup.ctx, setup.logged
    messages: list[dict[str, Any]] = [{"role": "user", "content": setup.first_message}]
    steps = 0
    model_id = ""
    stop_reason = "end_turn"
    wrap_up = ""
    stalled = 0  # consecutive steps whose tool calls all failed
    notified: set[str] = set()  # wrap-up notices already sent: "budget", "stall"
    while True:
        if steps >= case.budget_steps:
            stop_reason = "step_budget"
            break
        if logged.cost >= case.budget_usd:
            stop_reason = "cost_budget"
            break
        resp = logged.complete(system=setup.system, messages=messages, tools=setup.tools)
        steps += 1
        model_id = resp.model_id or model_id
        if not resp.tool_calls:
            stop_reason = "end_turn"
            wrap_up = resp.text.strip()[:WRAP_UP_MAX_CHARS]
            break
        blocks: list[dict[str, Any]] = []
        if resp.text:
            blocks.append({"type": "text", "text": resp.text})
        blocks += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
            for c in resp.tool_calls
        ]
        messages.append({"role": "assistant", "content": blocks})
        results: list[dict[str, Any]] = []
        for c in resp.tool_calls:
            out = call_tool(ctx, c["name"], c["input"])
            payload, is_error = tool_payload(ctx, c["name"], out)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": c["id"],
                    "content": wrap_tool_output(out.call_id, c["name"], payload),
                    "is_error": is_error,
                }
            )
        stalled = stalled + 1 if all(r["is_error"] for r in results) else 0
        if (text := wrap_up_notice(case, steps, stalled, notified)) is not None:
            results.append({"type": "text", "text": text})
        messages.append({"role": "user", "content": results})
    return RunOutcome(stop_reason, steps, model_id, wrap_up)


def investigate(
    conn: Conn,
    case: Case,
    policy: Policy,
    client: ModelClient,
    *,
    pricing: Pricing,
    workdir: Path,
    verifier: Verifier = default_verifier,
    question: str | None = None,
    document_client: ModelClient | None = None,
    evidence_store: EvidenceStore | None = None,
    web_provider: Any = None,
    signal_provider: Any = None,
    engine: str | None = None,
) -> InvestigationResult:
    engine = engine or engine_from_env()
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    plan = _approved_plan(conn, case)
    baseline = run_baseline(conn, case, policy)
    start_hash = ledger_input_hash(load_ledger(conn, case.borrower_id).known(case.as_of))
    if document_client is not None and not isinstance(document_client, LoggedClient):
        # Document reads are model calls too: log them (and their cost) against the case.
        document_client = LoggedClient(
            document_client, conn, case, load_prompt("document_reader", 1), pricing
        )
    ctx = open_context(
        conn, case, policy, workdir, verifier,
        document_client=document_client,
        evidence_store=evidence_store,
        web_provider=web_provider,
        signal_provider=signal_provider,
    )  # fmt: skip
    system, prompts = _system_text("system", "investigate")
    summary = _baseline_summary(conn, case, list(baseline.metric_ids.values()))
    reference = {"library": _catalog(policy), "snapshot_tables": schema_summary()}
    setup = RunSetup(
        case,
        ctx,
        LoggedClient(client, conn, case, prompts[-1], pricing),
        system,
        tool_definitions(ctx),
        _first_message(plan, summary, reference, question, _borrower_name(conn, case)),
        search_brief(ctx, _borrower_name(conn, case)) if signal_provider is not None else "",
    )
    set_case_status(conn, case.id, "investigating")

    if engine == "strands":
        from tangible.agent.strands_engine import run_strands

        outcome = run_strands(setup)
    else:
        outcome = run_classic(setup)

    if ledger_input_hash(load_ledger(conn, case.borrower_id).known(case.as_of)) != start_hash:
        # The run recorded public signals: metrics and rule checks for the new ledger state,
        # so the case can be signed on what is now known.
        refresh_baseline(conn, case, policy)

    cost = setup.logged.cost.quantize(Decimal("0.000001"))
    conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, object_kind, object_id, detail) "
        "VALUES (%s, 'agent', 'investigation_stopped', 'case', %s, %s::jsonb)",
        (
            case.tenant_id,
            case.id,
            json.dumps(
                {
                    "stop_reason": outcome.stop_reason,
                    "steps": outcome.steps,
                    "cost_usd": str(cost),
                    "engine": engine,
                }
                | ({"wrap_up": outcome.wrap_up} if outcome.wrap_up else {})
                | outcome.detail
            ),
        ),
    )
    set_case_status(conn, case.id, "review")
    model = [
        f for f in load_findings(conn, case_id=case.id, current_only=True) if f.origin == "model"
    ]
    return InvestigationResult(
        outcome.stop_reason, outcome.steps, cost,
        [f.id for f in model],
        [f.id for f in model if f.verified is False],
        dict(PROMPT_VERSIONS), outcome.model_id,
    )  # fmt: skip


def send_back(
    conn: Conn,
    case: Case,
    policy: Policy,
    client: ModelClient,
    question: str,
    user_id: str,
    *,
    pricing: Pricing,
    workdir: Path,
    verifier: Verifier = default_verifier,
    document_client: ModelClient | None = None,
    evidence_store: EvidenceStore | None = None,
    web_provider: Any = None,
    signal_provider: Any = None,
    engine: str | None = None,
) -> InvestigationResult:
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, case_id, user_id, action, reason) "
        "VALUES (%s, %s, %s, 'request_more', %s)",
        (case.tenant_id, case.id, user_id, question),
    )
    return investigate(
        conn, case, policy, client,
        pricing=pricing, workdir=workdir, verifier=verifier, question=question,
        document_client=document_client, evidence_store=evidence_store,
        web_provider=web_provider, signal_provider=signal_provider, engine=engine,
    )  # fmt: skip
