"""Wave 1 gate: every planted problem is caught by the right rule check at the right severity,
clean data raises nothing above info, and policy blocks a decision exactly when a hold fires.

Regression gate for every later wave: change a check or metric, and this must still pass.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.checks.registry import FindingDraft, run_check
from tangible.checks.sets import check_set
from tangible.datasets.synthetic import Profile, generate
from tangible.ledger.csv_dir import read_ledger_dir
from tangible.ledger.types import Ledger
from tangible.metrics import compute
from tangible.metrics.sets import metric_set
from tangible.policy import FindingInput, evaluate, load_policy
from test_synthetic import write_lines

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
    return [f for key, v in check_set("cs-4") for f in run_check(key, v, ledger, as_of)]


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


@pytest.mark.parametrize("plant", ["", *sorted(EXPECTED)])
def test_cs7_finds_what_cs6_finds_and_writes_it_in_dollars(synthetic_ledger, plant: str) -> None:
    """cs-7's checks are cs-6's with new wording: same findings, severities, facts and evidence;
    summaries in dollars and to two decimals. cs-6 texts stay as frozen assessments hold them."""
    ledger = synthetic_ledger(plant={plant} - {""})
    as_of = as_of_for(ledger)

    def run(set_id: str) -> list[FindingDraft]:
        return [f for key, v in check_set(set_id) for f in run_check(key, v, ledger, as_of)]

    def same(f: FindingDraft) -> tuple[object, ...]:
        return (f.check_key, f.severity, f.pillar, f.evidence_refs, dict(f.facts))

    old, new = run("cs-6"), run("cs-7")
    assert [same(f) for f in new] == [same(f) for f in old]
    for f in new:
        assert "GBP" not in f.summary and "£" not in f.summary, f.summary
    for o, n in zip(old, new, strict=True):
        if "GBP " in o.summary:
            assert "$" in n.summary and n.summary != o.summary
    if plant:
        assert any("$" in f.summary for f in new if f.check_key == EXPECTED[plant][0])


@pytest.mark.parametrize("days", [184, 187, 213])
def test_books_vs_processor_v2_does_not_judge_a_month_before_it_settles(
    synthetic_ledger, days: int
) -> None:
    """Clean data whose last day falls on the 4th to 7th of a month: v1 (cs-3) reads the unsettled
    tail of the previous month as a gap; v2 (cs-4) waits."""
    ledger = synthetic_ledger(days=days)
    as_of = as_of_for(ledger)
    assert as_of.day - 1 <= 7  # last bank day early in the month
    v1 = run_check("books_vs_processor", 1, ledger, as_of)
    assert v1 and v1[0].severity in ("review", "hold")
    assert run_check("books_vs_processor", 2, ledger, as_of) == []
    assert ("books_vs_processor", 1) in check_set("cs-3")
    assert ("books_vs_processor", 2) in check_set("cs-4")


# ---------------------------------------------------------------- distinct borrower identities

PROFILE = Profile(
    account_ref="GB-ZZZ-001",
    processor_name="STRIPE PAYMENTS UK",
    processor_source="stripe",
    owner_account="ADA QUILL PERSONAL",
    lender_name="TALLOW STREET CAPITAL",
    marketplace_name="AMAZON PAYMENTS UK",
    bank_name="FENLAND BANK PLC",
    landlord="STATION YARD LTD",
    supplier_names=tuple(f"SUPPLIER {i} LTD" for i in range(7)),
)


@pytest.fixture(scope="module")
def profiled_ledger(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Ledger]:
    """Like the `synthetic_ledger` fixture, but for a merchant with a non-default identity."""
    cache: dict[frozenset[str], Ledger] = {}

    def make(plant: Iterable[str] = ()) -> Ledger:
        key = frozenset(plant)
        if key not in cache:
            root = tmp_path_factory.mktemp("profiled")
            lines = write_lines(root / "lines.csv", days=200)
            generate(lines, root / "out", seed=7, plant=key, profile=PROFILE)
            cache[key] = read_ledger_dir(root / "out", lines)
        return cache[key]

    return make


def test_clean_with_a_distinct_profile_raises_nothing_above_info(
    profiled_ledger: Callable[..., Ledger],
) -> None:
    ledger = profiled_ledger()
    findings = all_findings(ledger, as_of_for(ledger))
    assert [(f.check_key, f.severity) for f in findings if f.severity != "info"] == []


@pytest.mark.parametrize("plant", sorted(EXPECTED))
def test_plant_is_caught_with_a_distinct_profile(
    profiled_ledger: Callable[..., Ledger], plant: str
) -> None:
    """The checks key on names (processor and marketplace keywords, OWNER DRAWINGS, lender
    words); a renamed merchant must trip the same check at the same severity."""
    ledger = profiled_ledger(plant={plant})
    findings = all_findings(ledger, as_of_for(ledger))
    key, severity = EXPECTED[plant]
    hits = [f for f in findings if f.check_key == key]
    assert hits and max((f.severity for f in hits), key=RANK.__getitem__) == severity, [
        (f.check_key, f.severity) for f in findings
    ]
    assert all(f.evidence_refs for f in hits)
    holds = {f.check_key for f in findings if f.severity == "hold"}
    assert holds <= ({key} if severity == "hold" else set())
    if plant == "undisclosed_lender_debit":
        assert hits[0].facts["counterparty"] == "TALLOW STREET CAPITAL"
        assert hits[0].facts["lender_like_name"] == "true"
    if plant == "self_transfer_revenue":
        assert hits[0].facts["counterparties"] == "ADA QUILL PERSONAL"
    if plant == "partial_coverage":
        assert hits[0].facts["unconnected_sources"] == "AMAZON"
        assert hits[0].facts["connected_sources"] == "STRIPE"
