"""Replay: re-derive a frozen assessment from the ledger as of its date and compare.

Model findings are carried as frozen. Re-running logged tool calls (not the agent) is out of
scope here.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.assessment.freeze import finding_inputs, review_states
from tangible.assessment.hashing import canonical_json, evidence_set_hash, ledger_input_hash
from tangible.checks.registry import FindingDraft, run_check
from tangible.checks.sets import check_set
from tangible.db.findings import StoredFinding, load_findings
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.metrics import compute
from tangible.metrics.sets import metric_set
from tangible.policy import Policy, evaluate, load_policy


@dataclass(frozen=True)
class ReplayReport:
    match: bool
    diffs: list[str] = field(default_factory=list)
    assessment_id: str = ""


def _finding_sig(d: FindingDraft) -> str:
    return canonical_json(
        {
            "check_key": d.check_key,
            "version": d.check_version,
            "severity": d.severity,
            "summary": d.summary,
            "facts": dict(d.facts),
            "refs": list(d.evidence_refs),
        }
    )


def _diff_findings(frozen: list[FindingDraft], fresh: list[FindingDraft]) -> list[str]:
    a = sorted(_finding_sig(d) for d in frozen)
    b = sorted(_finding_sig(d) for d in fresh)
    if a == b:
        return []
    by_sig = {_finding_sig(d): d for d in [*frozen, *fresh]}
    out: list[str] = []
    for sig in sorted(set(a) - set(b)):
        d = by_sig[sig]
        out.append(f"rule finding frozen but not reproduced: {d.check_key} v{d.check_version}")
    for sig in sorted(set(b) - set(a)):
        d = by_sig[sig]
        out.append(f"rule finding reproduced but not frozen: {d.check_key} v{d.check_version}")
    if not out:  # same signatures, different multiplicity
        out.append("rule finding counts differ")
    return out


def frozen_policy(policy_path: Path, frozen_hash: str) -> Policy:
    """The policy an assessment was frozen under. `policy_path` is the current default; when its
    hash differs, a policy file beside it with the frozen hash is used instead (older versions
    are kept as e.g. policies/default-0.2.2.yaml). With no match, the given file is returned and
    replay reports the policy change."""
    current = load_policy(policy_path)
    if current.sha256 == frozen_hash:
        return current
    for candidate in sorted(policy_path.parent.glob("*.yaml")):
        if candidate.resolve() == policy_path.resolve():
            continue
        try:
            older = load_policy(candidate)
        except ValueError:
            continue
        if older.sha256 == frozen_hash:
            return older
    return current


def replay(conn: Conn, assessment_id: str, policy_path: Path) -> ReplayReport:
    row = conn.execute(
        "SELECT borrower_id, as_of, evidence_set_hash, metric_set_version, policy_hash, "
        "decision_band, suggested_limit, rules_fired, created_at FROM assessment WHERE id = %s",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"assessment {assessment_id} not found")
    borrower_id, as_of, ev_hash, ms_id, policy_hash, band, limit, rules_fired, created_at = row
    borrower_id = str(borrower_id)
    diffs: list[str] = []

    policy = frozen_policy(Path(policy_path), policy_hash)
    assert policy.parsed is not None

    ledger = load_ledger(conn, borrower_id)
    known = ledger.known(as_of)
    input_hash = ledger_input_hash(known)

    # ---- metrics
    frozen_metrics: dict[tuple[str, int], tuple[Decimal | None, str]] = {
        (str(k), int(v)): (val, str(h))
        for k, v, val, h in conn.execute(
            "SELECT mv.metric_key, mv.metric_version, mv.value, mv.input_hash "
            "FROM assessment_item ai JOIN metric_value mv ON mv.id = ai.ref_id "
            "WHERE ai.assessment_id = %s AND ai.kind = 'metric'",
            (assessment_id,),
        ).fetchall()
    }
    pinned = metric_set(ms_id)
    for key, version in pinned:
        if (key, version) not in frozen_metrics:
            diffs.append(f"metric {key} v{version} missing from assessment")
            continue
        value, frozen_hash = frozen_metrics[(key, version)]
        if frozen_hash != input_hash:
            diffs.append(f"metric {key} v{version}: ledger input hash changed")
        fresh = compute(key, version, ledger, as_of)
        if fresh != value:
            diffs.append(f"metric {key} v{version}: frozen {value} != recomputed {fresh}")
    for key, version in sorted(set(frozen_metrics) - set(pinned)):
        diffs.append(f"metric {key} v{version} is not in pinned metric set {ms_id}")

    # ---- evidence set
    fresh_ev = evidence_set_hash(conn, known)
    if fresh_ev != ev_hash:
        diffs.append("evidence set hash changed: the evidence known at as_of differs")

    # ---- findings
    ids = [
        str(r[0])
        for r in conn.execute(
            "SELECT ref_id FROM assessment_item WHERE assessment_id = %s AND kind = 'finding'",
            (assessment_id,),
        ).fetchall()
    ]
    frozen_findings: list[StoredFinding] = []
    if ids:
        case_ids = {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT case_id FROM finding WHERE id = ANY(%s::uuid[])", (ids,)
            ).fetchall()
            if r[0] is not None
        }
        for case_id in sorted(case_ids):
            frozen_findings += [f for f in load_findings(conn, case_id=case_id) if f.id in ids]
    # Rule findings are re-derived with the policy file's check set (the assessment pins only the
    # metric set); a different policy is reported below.
    fresh_rule = [
        d
        for key, version in check_set(policy.parsed.check_set)
        for d in run_check(key, version, ledger, as_of)
    ]
    diffs += _diff_findings([f.draft for f in frozen_findings if f.origin == "rule"], fresh_rule)

    # ---- policy
    if policy.sha256 != policy_hash:
        diffs.append(f"policy changed: file hash {policy.sha256[:12]} != frozen {policy_hash[:12]}")
    else:
        states = review_states(conn, [f.id for f in frozen_findings], created_at)
        metrics_by_key: dict[str, Decimal | None] = {
            k: v for (k, _), (v, _) in frozen_metrics.items()
        }
        result = evaluate(policy, metrics_by_key, finding_inputs(frozen_findings, states))
        if result.band != band:
            diffs.append(f"decision band: frozen {band} != replayed {result.band}")
        if result.limit != limit:
            diffs.append(f"suggested limit: frozen {limit} != replayed {result.limit}")
        fired: Any = rules_fired
        if result.rules_fired != fired:
            diffs.append("rules fired differ")

    return ReplayReport(not diffs, diffs, str(assessment_id))


def replay_sample(conn: Conn, n: int, policy_path: Path) -> list[ReplayReport]:
    """Replay the n most recent assessments visible to this (tenant) connection."""
    ids = [
        str(r[0])
        for r in conn.execute(
            "SELECT id FROM assessment ORDER BY created_at DESC, id LIMIT %s", (n,)
        ).fetchall()
    ]
    return [replay(conn, i, policy_path) for i in ids]


__all__ = ["ReplayReport", "replay", "replay_sample"]
