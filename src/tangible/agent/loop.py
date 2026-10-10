"""The investigation loop: plan, analyst approval, then a budgeted tool-using model run.

The agent investigates, tools compute, policy decides. Nothing the model writes is executed or
trusted: its tool calls go through `call_tool` (validated, logged), and every tool result goes
back wrapped as quoted, untrusted data. Budgets are enforced here, not by the model.
"""

import html
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tangible.agent.llm import LoggedClient, ModelClient, Pricing
from tangible.agent.prompts import Prompt, load_prompt
from tangible.agent.tools import call_tool, open_context, tool_definitions
from tangible.agent.tools.runtime import Verifier
from tangible.agent.verify import verify_numbers
from tangible.assessment import run_baseline
from tangible.db.cases import Case, set_case_status
from tangible.db.findings import load_findings
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.policy import Policy

PROMPT_VERSIONS = {"system": "v1", "investigate": "v1", "plan": "v1"}


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
    prompts = [load_prompt(n, 1) for n in names]
    return "\n\n".join(p.text for p in prompts), prompts


def propose_plan(conn: Conn, case: Case, client: ModelClient, *, pricing: Pricing) -> PlanResult:
    system, prompts = _system_text("system", "plan")
    logged = LoggedClient(client, conn, case, prompts[-1], pricing)
    profile = json.dumps(borrower_profile(conn, case), indent=2)
    resp = logged.complete(
        system=system,
        messages=[{"role": "user", "content": f"Borrower profile (tool-computed):\n{profile}"}],
        tools=None,
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
    findings = load_findings(conn, case_id=case.id)
    lines = ["Baseline metrics (tool-computed):"]
    lines += [f"- {k} v{v}: {val if val is not None else 'n/a'}" for k, v, val in metrics]
    lines.append("Baseline rule findings:")
    rules = [f for f in findings if f.origin == "rule"]
    lines += [f"- [{f.draft.severity}] {f.draft.check_key}: {f.draft.summary}" for f in rules]
    if not rules:
        lines.append("- none")
    prior = [f for f in findings if f.origin == "model"]
    if prior:
        lines.append("Findings already recorded by earlier agent runs:")
        lines += [
            f"- [{f.draft.severity}] {f.draft.check_key}: {f.draft.summary} (verified={f.verified})"
            for f in prior
        ]
    return "\n".join(lines)


def _first_message(plan: dict[str, Any], baseline: str, question: str | None) -> str:
    parts = ["Approved investigation plan:", json.dumps(plan, indent=2), "", baseline]
    if question:
        parts += [
            "",
            "Question from the analyst (trusted instruction from the underwriter):",
            question,
        ]
    return "\n".join(parts)


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
) -> InvestigationResult:
    plan = _approved_plan(conn, case)
    baseline = run_baseline(conn, case, policy)
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
    )  # fmt: skip
    system, prompts = _system_text("system", "investigate")
    logged = LoggedClient(client, conn, case, prompts[-1], pricing)
    tools = tool_definitions()
    summary = _baseline_summary(conn, case, list(baseline.metric_ids.values()))
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _first_message(plan, summary, question)}
    ]
    set_case_status(conn, case.id, "investigating")

    steps = 0
    model_id = ""
    stop_reason = "end_turn"
    while True:
        if steps >= case.budget_steps:
            stop_reason = "step_budget"
            break
        if logged.cost >= case.budget_usd:
            stop_reason = "cost_budget"
            break
        resp = logged.complete(system=system, messages=messages, tools=tools)
        steps += 1
        model_id = resp.model_id or model_id
        if not resp.tool_calls:
            stop_reason = "end_turn"
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
            if out.error is not None:
                payload, is_error = out.error, True
            else:
                payload, is_error = json.dumps(out.result, ensure_ascii=False), False
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": c["id"],
                    "content": wrap_tool_output(out.call_id, c["name"], payload),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    cost = logged.cost.quantize(Decimal("0.000001"))
    conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, object_kind, object_id, detail) "
        "VALUES (%s, 'agent', 'investigation_stopped', 'case', %s, %s::jsonb)",
        (
            case.tenant_id,
            case.id,
            json.dumps({"stop_reason": stop_reason, "steps": steps, "cost_usd": str(cost)}),
        ),
    )
    set_case_status(conn, case.id, "review")
    model = [f for f in load_findings(conn, case_id=case.id) if f.origin == "model"]
    return InvestigationResult(
        stop_reason, steps, cost,
        [f.id for f in model],
        [f.id for f in model if f.verified is False],
        dict(PROMPT_VERSIONS), model_id,
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
        web_provider=web_provider,
    )  # fmt: skip
