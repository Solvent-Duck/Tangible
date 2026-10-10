"""Baseline: compute every metric in the policy's metric set and run every rule check, once."""

from dataclasses import dataclass

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.assessment.hashing import ledger_input_hash
from tangible.checks.registry import run_check
from tangible.checks.sets import check_set
from tangible.db.cases import Case
from tangible.db.findings import insert_finding, load_findings
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.metrics import compute
from tangible.metrics.sets import metric_set
from tangible.policy import Policy


@dataclass(frozen=True)
class Baseline:
    metric_ids: dict[tuple[str, int], str]  # (metric_key, version) -> metric_value.id
    finding_ids: list[str]  # rule findings on the case


def run_baseline(conn: Conn, case: Case, policy: Policy) -> Baseline:
    if policy.parsed is None:
        raise ValueError("Policy has no parsed spec")
    ledger = load_ledger(conn, case.borrower_id)
    input_hash = ledger_input_hash(ledger.known(case.as_of))

    metric_ids: dict[tuple[str, int], str] = {}
    for key, version in metric_set(policy.parsed.metric_set):
        value = compute(key, version, ledger, case.as_of)
        conn.execute(
            "INSERT INTO metric_value (tenant_id, borrower_id, metric_key, metric_version, "
            "as_of, value, input_hash) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (borrower_id, metric_key, metric_version, as_of, input_hash) DO NOTHING",
            (case.tenant_id, case.borrower_id, key, version, case.as_of, value, input_hash),
        )
        row = conn.execute(
            "SELECT id FROM metric_value WHERE borrower_id = %s AND metric_key = %s "
            "AND metric_version = %s AND as_of = %s AND input_hash = %s",
            (case.borrower_id, key, version, case.as_of, input_hash),
        ).fetchone()
        assert row
        metric_ids[(key, version)] = str(row[0])

    existing = [f for f in load_findings(conn, case_id=case.id) if f.origin == "rule"]
    if existing:
        return Baseline(metric_ids, [f.id for f in existing])
    finding_ids = [
        insert_finding(
            conn, case.tenant_id, case.borrower_id, draft, origin="rule", case_id=case.id
        )
        for key, version in check_set(policy.parsed.check_set)
        for draft in run_check(key, version, ledger, case.as_of)
    ]
    return Baseline(metric_ids, finding_ids)
