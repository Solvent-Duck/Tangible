"""Freeze: evaluate policy over the baseline and write an immutable assessment.

Overrides never touch the assessment; they are underwriter_action rows beside it.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tangible.assessment.hashing import evidence_set_hash, ledger_input_hash
from tangible.db.cases import Case, get_case, set_case_status
from tangible.db.findings import StoredFinding, load_findings
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.metrics.sets import metric_set
from tangible.policy import (
    ActionInput,
    FindingInput,
    Policy,
    PolicyResult,
    ReviewState,
    effective_state,
    evaluate,
)


@dataclass(frozen=True)
class Frozen:
    assessment_id: str
    result: PolicyResult


def review_states(
    conn: Conn, finding_ids: list[str], at_or_before: datetime
) -> dict[str, ReviewState]:
    """Review state per finding from underwriter_action rows with `at <= at_or_before`."""
    actions: dict[str, list[ActionInput]] = {fid: [] for fid in finding_ids}
    if finding_ids:
        for fid, action, at in conn.execute(
            "SELECT finding_id, action, at FROM underwriter_action "
            "WHERE finding_id = ANY(%s::uuid[]) AND at <= %s",
            (finding_ids, at_or_before),
        ).fetchall():
            actions[str(fid)].append(ActionInput(action, at))
    return {fid: effective_state(a) for fid, a in actions.items()}


def finding_inputs(
    findings: list[StoredFinding], states: dict[str, ReviewState]
) -> list[FindingInput]:
    return [FindingInput(f.id, f.draft.check_key, f.draft.severity, states[f.id]) for f in findings]


def freeze(
    conn: Conn,
    case: Case,
    policy: Policy,
    *,
    created_by: str,
    model_id: str | None = None,
    prompt_versions: dict[str, str] | None = None,
) -> Frozen:
    if policy.parsed is None:
        raise ValueError("Policy has no parsed spec")
    if get_case(conn, case.id).status == "signed":
        raise ValueError(f"case {case.id} is already signed and frozen")

    known = load_ledger(conn, case.borrower_id).known(case.as_of)
    input_hash = ledger_input_hash(known)

    # The baseline's metric rows are the ones for this ledger state; none means no (current)
    # baseline.
    metric_rows: list[tuple[str, str, Decimal | None]] = []
    for key, version in metric_set(policy.parsed.metric_set):
        row = conn.execute(
            "SELECT id, value FROM metric_value WHERE borrower_id = %s AND metric_key = %s "
            "AND metric_version = %s AND as_of = %s AND input_hash = %s",
            (case.borrower_id, key, version, case.as_of, input_hash),
        ).fetchone()
        if row is None:
            raise ValueError(f"case {case.id} has no baseline for metric {key} v{version}")
        metric_rows.append((str(row[0]), key, row[1]))

    findings = load_findings(conn, case_id=case.id)
    now_row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert now_row
    now: datetime = now_row[0]
    states = review_states(conn, [f.id for f in findings], now)

    result = evaluate(
        policy, {key: value for _, key, value in metric_rows}, finding_inputs(findings, states)
    )

    row = conn.execute(
        "INSERT INTO assessment (tenant_id, borrower_id, as_of, evidence_set_hash, "
        "metric_set_version, policy_version, policy_hash, model_id, prompt_versions, "
        "decision_band, suggested_limit, rules_fired, created_at, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            case.tenant_id, case.borrower_id, case.as_of, evidence_set_hash(conn, known),
            policy.parsed.metric_set, policy.version, policy.sha256, model_id,
            json.dumps(prompt_versions or {}), result.band, result.limit,
            json.dumps(result.rules_fired), now, created_by,
        ),
    ).fetchone()  # fmt: skip
    assert row
    assessment_id = str(row[0])

    items = [("metric", mid) for mid, _, _ in metric_rows] + [("finding", f.id) for f in findings]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO assessment_item (tenant_id, assessment_id, kind, ref_id) "
            "VALUES (%s, %s, %s, %s)",
            [(case.tenant_id, assessment_id, kind, ref) for kind, ref in items],
        )
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, user_id, action, reason, case_id) "
        "VALUES (%s, %s, 'sign', 'signed and frozen', %s)",
        (case.tenant_id, created_by, case.id),
    )
    set_case_status(conn, case.id, "signed")
    return Frozen(assessment_id, result)


def record_override(conn: Conn, assessment_id: str, user_id: str, reason: str) -> str:
    """An override sits beside the assessment as an underwriter_action; the assessment is
    never modified."""
    row = conn.execute(
        "INSERT INTO underwriter_action (tenant_id, assessment_id, user_id, action, reason) "
        "SELECT tenant_id, id, %s, 'override', %s FROM assessment WHERE id = %s RETURNING id",
        (user_id, reason, assessment_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"assessment {assessment_id} not found")
    return str(row[0])
