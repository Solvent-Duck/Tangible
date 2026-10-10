import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tangible.backtest.cohort import COLUMNS, Loan, load_cohort
from tangible.backtest.report import SYNTHETIC_BANNER, build_report, render_markdown
from tangible.backtest.runner import (
    AgentRunInfo,
    BacktestResult,
    FindingRec,
    LeakCheck,
    LoanResult,
    as_of_for,
    leak_check,
    run_backtest,
)
from tangible.backtest.stats import (
    EXPECTED,
    auc,
    compute_stats,
    directed_auc,
    ks,
    loss_by_band,
    plant_catch,
)
from tangible.backtest.synthetic_cohort import build_synthetic_cohort
from tangible.db.cases import Case
from tangible.db.repo import load_ledger
from tangible.db.session import Conn, tenant_session
from tangible.policy import Policy
from test_synthetic import write_lines

POLICY_PATH = Path(__file__).parent.parent / "policies/default.yaml"


# ---------------------------------------------------------------- stats primitives (no db)


def test_auc_known_answers() -> None:
    assert auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert auc([4, 3, 2, 1], [False, False, True, True]) == 0.0
    assert auc([1, 1, 1, 1], [False, True, False, True]) == 0.5  # all ties
    assert auc([1, 2, 1, 2], [False, False, True, True]) == 0.5  # interleaved
    assert auc([1, 2, 3], [False, False, False]) is None
    # one tie, one win, one loss, one win: (0.5 + 1 + 0 + 1) / 4
    assert auc([2, 3], [True, False]) == 0.0
    assert auc([1, 2, 2, 3], [True, True, False, False]) == pytest.approx(0.125)


def test_directed_auc_flips_to_at_least_half() -> None:
    assert directed_auc([4, 3, 2, 1], [False, False, True, True]) == (1.0, "lower_is_riskier")
    assert directed_auc([1, 2, 3, 4], [False, False, True, True]) == (1.0, "higher_is_riskier")
    assert directed_auc([1], [True]) is None


def test_ks_known_answers() -> None:
    assert ks([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert ks([1, 1, 1, 1], [False, True, False, True]) == 0.0
    assert ks([1, 2, 3, 4], [False, True, False, True]) == pytest.approx(0.5)
    assert ks([1, 2], [True, True]) is None


def fake_loan(
    key: str, outcome: str, planted: tuple[str, ...] = (), loss: str = "0", amount: str = "100"
) -> Loan:
    from datetime import date

    return Loan(key, Path("."), Path("."), date(2011, 1, 1), Decimal(amount), outcome,
                Decimal(loss), True, planted)  # fmt: skip


def fake_result(
    loan: Loan,
    band: str | None,
    findings: tuple[tuple[str, str], ...] = (),
    metrics: dict[str, Decimal | None] | None = None,
    hidden: int = 3,
) -> LoanResult:
    return LoanResult(
        loan, band, None, metrics or {}, tuple(FindingRec(k, s, "rule") for k, s in findings),
        True, (), LeakCheck(10, 7, hidden, 0), "c", "a",
    )  # fmt: skip


def test_loss_by_band_and_catch_on_toy_results() -> None:
    loans = [
        fake_result(fake_loan("a", "repaid"), "A"),
        fake_result(fake_loan("b", "repaid"), "A"),
        fake_result(fake_loan("c", "defaulted", loss="60"), "C"),
        fake_result(fake_loan("d", "repaid"), "C"),
        fake_result(
            fake_loan("e", "defaulted", ("hidden_reserve",), loss="60"),
            None,
            (("payouts_vs_deposits", "hold"),),
        ),
        fake_result(fake_loan("f", "defaulted", ("phantom_payouts",), loss="60"), "C"),
    ]
    by = loss_by_band(loans)
    assert (by["A"]["count"], by["A"]["defaults"], by["A"]["default_rate"]) == (2, 0, 0.0)
    assert (by["C"]["count"], by["C"]["defaults"], by["C"]["loss_amount"]) == (3, 2, 120.0)
    assert (by["none"]["count"], by["none"]["default_rate"]) == (1, 1.0)
    assert by["B"]["default_rate"] is None
    pc = plant_catch(loans)
    assert (pc["planted"], pc["caught"]) == (2, 1)
    assert pc["misses"] == [{"borrower_key": "f", "plant": "phantom_payouts"}]
    st = compute_stats(loans)
    assert st["agent"] == {"status": "not run"}
    assert st["false_alarms"]["clean_loans"] == 4 and st["false_alarms"]["alarms"] == 0


def test_metric_stats_none_share_and_lift() -> None:
    loans = [
        fake_result(fake_loan("a", "repaid"), "A", metrics={"m": Decimal(1)}),
        fake_result(fake_loan("b", "repaid"), "A", metrics={"m": Decimal(2)}),
        fake_result(fake_loan("c", "defaulted"), "D", (("x", "review"),), {"m": Decimal(8)}),
        fake_result(fake_loan("d", "defaulted"), "D", (("x", "info"),), {"m": None}),
    ]
    st = compute_stats(loans)
    m = st["metrics"]["m"]
    assert m["none_share"] == 0.25 and m["auc"] == 1.0 and m["ks"] == 1.0
    assert m["direction"] == "higher_is_riskier"
    lift = st["finding_lift"]["x"]  # info findings do not count as present
    assert (lift["present"], lift["default_rate_present"], lift["default_rate_absent"]) == (
        1,
        1.0,
        pytest.approx(1 / 3),
    )


def test_cohort_validation(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "l.csv").write_text("x\n")
    header = ",".join(COLUMNS) + "\n"

    def load(row: str) -> list[Loan]:
        p = tmp_path / "cohort.csv"
        p.write_text(header + row + "\n")
        return load_cohort(p)

    ok = load("k,d,l.csv,2011-01-01,100.00,defaulted,60.00,true,a;b")
    assert ok[0].planted == ("a", "b") and ok[0].defaulted and ok[0].data_dir == tmp_path / "d"
    for bad, msg in (
        ("k,d,l.csv,2011-13-01,100,repaid,0,true,", "decision_date"),
        ("k,d,l.csv,2011-01-01,100,lost,0,true,", "outcome"),
        ("k,d,l.csv,2011-01-01,100,repaid,5,true,", "repaid"),
        ("k,d,l.csv,2011-01-01,100,defaulted,500,true,", "exceeds"),
        ("k,d,l.csv,2011-01-01,abc,repaid,0,true,", "not a number"),
        ("k,d,l.csv,2011-01-01,100,repaid,0,maybe,", "synthetic"),
        ("k,nope,l.csv,2011-01-01,100,repaid,0,true,", "does not exist"),
    ):
        with pytest.raises(ValueError, match=msg):
            load(bad)
    (tmp_path / "cohort.csv").write_text("borrower_key\nk\n")
    with pytest.raises(ValueError, match="missing column"):
        load_cohort(tmp_path / "cohort.csv")


# ---------------------------------------------------------------- end to end (db)


@dataclass
class Built:
    cohort_path: Path
    loans: list[Loan]
    result: BacktestResult


@pytest.fixture(scope="module")
def built(test_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Built:
    root = tmp_path_factory.mktemp("backtest")
    lines = write_lines(root / "lines.csv", days=200)
    path = build_synthetic_cohort(lines, root / "cohort", n_borrowers=6, seed=7, plant_rate=0.5)
    loans = load_cohort(path)
    result = run_backtest(test_db_url, "bt-main", loans, POLICY_PATH, root / "store")
    return Built(path, loans, result)


@pytest.mark.db
def test_synthetic_cohort_shape(built: Built) -> None:
    loans = built.loans
    assert len(loans) == 6
    assert all(ln.synthetic for ln in loans)
    planted = [ln for ln in loans if ln.planted]
    assert len(planted) == 3 and all(1 <= len(ln.planted) <= 2 for ln in planted)
    assert all(ln.defaulted == bool(ln.planted) for ln in loans)
    assert all(ln.loss_amount > 0 for ln in planted)
    assert all(ln.loss_amount == 0 for ln in loans if not ln.planted)
    assert all(ln.loan_amount > 0 for ln in loans)
    # partitions are disjoint: no invoice appears in two partitions
    seen: dict[str, str] = {}
    for ln in loans:
        with ln.lines_csv.open(newline="") as f:
            for row in csv.DictReader(f):
                assert seen.setdefault(row["invoice"], ln.borrower_key) == ln.borrower_key


@pytest.mark.db
def test_end_to_end_report_and_headline(built: Built) -> None:
    res = built.result
    report = build_report(res)
    md = render_markdown(report)
    assert md.splitlines()[0] == SYNTHETIC_BANNER
    for section in (
        "## Cohort summary", "## Loss by band", "## Metric discrimination", "## Finding lift",
        "## Plants caught", "## False alarms", "## Replay matches", "## Time-leak guard",
        "## Agent spread and cost",
    ):  # fmt: skip
        assert section in md
    assert "Agent: not run." in md
    parsed = json.loads(json.dumps(report))
    assert parsed["synthetic"] is True and len(parsed["loans"]) == 6

    stats = report["stats"]
    # every planted loan's expected check fired
    assert stats["plants"]["planted"] >= 3
    assert stats["plants"]["catch_rate"] == 1.0, stats["plants"]["misses"]
    # replay matches everywhere
    assert stats["replay"]["matched"] == 6
    # clean loans get a band; planted hold plants get none
    for r in res.loans:
        holds = {EXPECTED[p][0] for p in r.loan.planted if EXPECTED[p][1] == "hold"}
        if not r.loan.planted:
            assert r.band is not None and r.limit is not None
        if holds:
            assert r.band is None and r.limit is None
    assert any(r.band is None for r in res.loans) or not any(
        EXPECTED[p][1] == "hold" for r in res.loans for p in r.loan.planted
    )
    assert stats["agent"] == {"status": "not run"}


@pytest.mark.db
def test_time_leak_guard_and_earlier_dates(built: Built, test_db_url: str) -> None:
    res = built.result
    assert res.loans and all(r.leak.violations == 0 for r in res.loans)
    assert all(r.leak.hidden_rows > 0 for r in res.loans)  # data runs past the decision date

    loan = res.loans[0].loan
    with tenant_session(test_db_url, res.tenant_id) as conn:
        row = conn.execute(
            "SELECT id FROM borrower WHERE name = %s", (loan.borrower_key,)
        ).fetchone()
        assert row
        ledger = load_ledger(conn, str(row[0]))
    as_of = as_of_for(loan)
    prev: LeakCheck | None = None
    for back in (0, 20, 60, 120):
        when = as_of - timedelta(days=back)
        chk = leak_check(ledger, when)
        known = ledger.known(when)
        assert chk.violations == 0
        for name in ("orders", "refunds", "payouts", "bank_txns", "invoices", "account_balances"):
            assert all(r.observed_at <= when for r in getattr(known, name))
        if prev is not None:
            assert chk.visible_rows < prev.visible_rows
            assert chk.hidden_rows > prev.hidden_rows
        prev = chk
    # a decision date before the data starts sees nothing
    first = datetime.combine(as_of.date() - timedelta(days=5000), time(0), UTC)
    assert leak_check(ledger, first).visible_rows == 0


@pytest.mark.db
def test_deterministic(built: Built, test_db_url: str, tmp_path: Path) -> None:
    again = run_backtest(test_db_url, "bt-again", built.loans, POLICY_PATH, tmp_path / "store")
    a = [(r.loan.borrower_key, r.band, r.limit, r.metrics) for r in built.result.loans]
    b = [(r.loan.borrower_key, r.band, r.limit, r.metrics) for r in again.loans]
    assert a == b
    assert [sorted((f.check_key, f.severity) for f in r.findings) for r in built.result.loans] == [
        sorted((f.check_key, f.severity) for f in r.findings) for r in again.loans
    ]


@pytest.mark.db
def test_agent_hook_called_per_run_and_aggregated(
    built: Built, test_db_url: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_agent(conn: Conn, case: Case, policy: Policy) -> AgentRunInfo:
        assert conn.execute("SELECT count(*) FROM finding WHERE case_id = %s", (case.id,))
        calls.append(case.id)
        return AgentRunInfo(Decimal("0.25"), 4, ())

    subset = built.loans[:2]
    res = run_backtest(
        test_db_url, "bt-agent", subset, POLICY_PATH, tmp_path / "store", agent=fake_agent,
        agent_runs=3,
    )  # fmt: skip
    assert len(calls) == 6 and len(set(calls)) == 6
    ag = compute_stats(res.loans, res.agent_requested_runs)["agent"]
    assert ag["status"] == "run" and ag["cases"] == 6
    assert ag["total_cost_usd"] == pytest.approx(1.5)
    assert ag["cost_per_case_usd"] == pytest.approx(0.25) and ag["steps_per_case"] == 4
    assert ag["loans_all_runs_agree"] == 1.0 and ag["pairwise_band_agreement"] == 1.0
    assert all(r.replay_match for r in res.loans)
    assert "Cost per case" in render_markdown(build_report(res))
