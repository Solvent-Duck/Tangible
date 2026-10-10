"""Wave 1 gate: every planted problem is caught by the right rule check at the right severity,
clean data raises nothing above info, and policy blocks a decision exactly when a hold fires.

Regression gate for every later wave: change a check or metric, and this must still pass.
"""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.checks.registry import FindingDraft, run_check
from tangible.checks.sets import check_set
from tangible.ledger.types import Ledger
from tangible.metrics import compute
from tangible.metrics.sets import metric_set
from tangible.policy import FindingInput, evaluate, load_policy

POLICY = Path(__file__).parent.parent / "policies/default.yaml"
RANK = {"info": 0, "review": 1, "hold": 2}

# plant -> (check that must fire, its severity)
EXPECTED: dict[str, tuple[str, str]] = {
    "hidden_reserve": ("payouts_vs_deposits", "hold"),
    "phantom_payouts": ("orders_vs_payouts", "review"),
    "partial_coverage": ("partial_coverage", "hold"),
    "self_transfer_revenue": ("self_transfers_as_revenue", "review"),
    "undisclosed_lender_debit": ("unexplained_recurring_debit", "review"),
}


def as_of_for(ledger: Ledger) -> datetime:
    last = max(t.posted_on for t in ledger.bank_txns)
    return datetime.combine(last + timedelta(days=1), time(12), tzinfo=UTC)


def all_findings(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    return [f for key, v in check_set("cs-2") for f in run_check(key, v, ledger, as_of)]


def decide(ledger: Ledger, as_of: datetime, findings: list[FindingDraft]):  # noqa: ANN201
    policy = load_policy(POLICY)
    assert policy.parsed is not None
    metrics = {k: compute(k, v, ledger, as_of) for k, v in metric_set(policy.parsed.metric_set)}
    inputs = [
        FindingInput(f"f{i}", f.check_key, f.severity, "open") for i, f in enumerate(findings)
    ]
    return evaluate(policy, metrics, inputs)


def test_clean_raises_nothing_above_info_and_gets_a_decision(synthetic_ledger) -> None:
    ledger = synthetic_ledger()
    as_of = as_of_for(ledger)
    findings = all_findings(ledger, as_of)
    loud = [(f.check_key, f.severity) for f in findings if f.severity != "info"]
    assert loud == []
    result = decide(ledger, as_of, findings)
    assert result.band is not None and result.limit is not None and result.limit > 0


@pytest.mark.parametrize("plant", sorted(EXPECTED))
def test_plant_is_caught(synthetic_ledger, plant: str) -> None:
    ledger = synthetic_ledger(plant={plant})
    as_of = as_of_for(ledger)
    findings = all_findings(ledger, as_of)
    key, severity = EXPECTED[plant]
    hits = [f for f in findings if f.check_key == key]
    assert hits and max((f.severity for f in hits), key=RANK.__getitem__) == severity, [
        (f.check_key, f.severity) for f in findings
    ]
    assert all(f.evidence_refs for f in hits)
    # No hold except the one the plant should cause.
    holds = {f.check_key for f in findings if f.severity == "hold"}
    assert holds <= ({key} if severity == "hold" else set())
    result = decide(ledger, as_of, findings)
    if severity == "hold":
        assert result.band is None and result.limit is None
    else:
        assert result.band is not None and result.band >= "C"  # review cap applies
