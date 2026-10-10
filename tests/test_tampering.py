from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tangible.checks import tampering as t
from tangible.checks.registry import FindingDraft
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import Application, BankTxn, Invoice, Ledger, Order

AS_OF = datetime(2025, 6, 30, 12, tzinfo=UTC)
TODAY = AS_OF.date()
D = TODAY - timedelta(days=5)


def at(d: date, hour: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def bank(
    ref: str,
    day: date,
    amount: str = "10.00",
    direction: str = "in",
    cp: str | None = "CUSTOMER",
    desc: str | None = None,
    observed: datetime | None = None,
) -> BankTxn:
    return BankTxn(
        source_ref=ref,
        account_ref="A",
        amount=Decimal(amount),
        direction=direction,  # type: ignore[arg-type]
        counterparty=cp,
        description=desc,
        posted_on=day,
        observed_at=observed or at(day, 6),
        evidence=EvidenceRef("pull", ref),
    )


def invoice(
    ref: str,
    issued: date,
    amount: str = "100.00",
    paid: date | None = None,
    observed: datetime | None = None,
    kind: str = "payable",
    cp: str | None = "SUPPLIER",
) -> Invoice:
    return Invoice(
        source_ref=ref,
        kind=kind,  # type: ignore[arg-type]
        counterparty=cp,
        amount=Decimal(amount),
        issued_on=issued,
        due_on=None,
        paid_on=paid,
        observed_at=observed or at(issued, 1),
        evidence=EvidenceRef("pull", ref),
    )


def order(ref: str, day: date, gross: str, observed: datetime) -> Order:
    return Order(
        ref, Decimal(gross), Decimal(0), at(day, 10), observed, evidence=EvidenceRef("pull", ref)
    )


# ---------------------------------------------------------------- backdated_edits


def test_backdated_lag_boundary() -> None:
    biz = date(2025, 3, 1)
    ok = Ledger(bank_txns=(bank("b", biz, observed=at(biz + timedelta(days=35))),))
    assert t.backdated_edits(ok, AS_OF) == []
    bad = Ledger(bank_txns=(bank("b", biz, observed=at(biz + timedelta(days=36))),))
    out = t.backdated_edits(bad, AS_OF)
    assert len(out) == 1 and out[0].severity == "review"
    assert out[0].facts["late_observed_rows"] == "1" and out[0].facts["max_lag_days"] == "36"
    assert out[0].evidence_refs == ("pull#b",)


def test_backdated_orders_and_paid_invoice_uses_paid_on() -> None:
    biz = date(2025, 3, 1)
    late_order = Ledger(orders=(order("o", biz, "5", at(biz + timedelta(days=40))),))
    assert len(t.backdated_edits(late_order, AS_OF)) == 1
    # issued long ago, paid recently, paid revision observed next morning: legitimate
    paid = date(2025, 6, 10)
    inv = Ledger(invoices=(invoice("i", date(2025, 3, 1), paid=paid, observed=at(paid, 6)),))
    assert t.backdated_edits(inv, AS_OF) == []


def test_edit_to_closed_period_boundary() -> None:
    first = invoice("i", date(2025, 3, 10))

    def led(days_after_close: int) -> Ledger:
        seen = at(date(2025, 3, 31) + timedelta(days=days_after_close), 3)
        return Ledger(invoices=(first, invoice("i", date(2025, 3, 10), "150.00", observed=seen)))

    # the revision is also seen late (the lag rule), so look at the edit count
    assert t.backdated_edits(led(35), AS_OF)[0].facts["closed_period_edits"] == "0"
    out = t.backdated_edits(led(36), AS_OF)
    assert len(out) == 1 and out[0].facts["closed_period_edits"] == "1"
    assert out[0].facts["max_days_after_close"] == "36"
    assert out[0].evidence_refs == ("pull#i",)


def test_edit_to_closed_period_needs_an_amount_change() -> None:
    o1 = order("o", date(2025, 3, 10), "10", at(date(2025, 3, 10), 11))
    same = order("o", date(2025, 3, 10), "10", at(date(2025, 6, 1)))
    out = t.backdated_edits(Ledger(orders=(o1, same)), AS_OF)  # the lag rule still sees it
    assert out[0].facts["closed_period_edits"] == "0"
    changed = order("o", date(2025, 3, 10), "99", at(date(2025, 6, 1)))
    out = t.backdated_edits(Ledger(orders=(o1, changed)), AS_OF)
    assert out[0].facts["closed_period_edits"] == "1"


def test_revision_after_as_of_is_invisible() -> None:
    first = invoice("i", date(2025, 3, 10))
    rev = invoice("i", date(2025, 3, 10), "150.00", observed=AS_OF + timedelta(seconds=1))
    assert t.backdated_edits(Ledger(invoices=(first, rev)), AS_OF) == []


# ---------------------------------------------------------------- round_number_pattern


def round_ledger(round_n: int, other_n: int) -> Ledger:
    rows = [bank(f"r{i}", D, "200.00") for i in range(round_n)]
    rows += [bank(f"o{i}", D, "10.55") for i in range(other_n)]
    return Ledger(bank_txns=tuple(rows))


def test_round_number_boundary() -> None:
    assert t.round_number_pattern(round_ledger(5, 20), AS_OF) == []  # exactly 20%
    out = t.round_number_pattern(round_ledger(5, 19), AS_OF)
    assert len(out) == 1 and out[0].severity == "info"
    assert out[0].facts["round_count"] == "5"
    assert t.round_number_pattern(round_ledger(4, 0), AS_OF) == []  # 100% but only 4 rows


# ---------------------------------------------------------------- self_transfers_as_revenue

DRAW = bank("d", D - timedelta(days=60), "1000.00", "out", "J SMITH PERSONAL", "OWNER DRAWINGS")


def self_ledger(own: str, app: Application | None = None, drawings: bool = False) -> Ledger:
    rows = [bank("own", D, own, cp="J SMITH PERSONAL"), bank("cust", D, "9800.00")]
    if drawings:
        rows.append(DRAW)
    return Ledger(bank_txns=tuple(rows), applications=(app,) if app else ())


def test_self_transfer_via_drawings_boundary() -> None:
    # 200.00 of 10000.00 is exactly 2%
    assert t.self_transfers_as_revenue(self_ledger("200.00", drawings=True), AS_OF) == []
    out = t.self_transfers_as_revenue(self_ledger("200.01", drawings=True), AS_OF)
    assert len(out) == 1 and out[0].severity == "review"
    assert out[0].facts["counterparties"] == "J SMITH PERSONAL"
    assert out[0].evidence_refs[0] == "pull#own"
    assert t.self_transfers_as_revenue(self_ledger("5000.00"), AS_OF) == []  # nothing marks them


def test_self_transfer_via_application_names() -> None:
    app = Application("a", None, ("ACME LTD CURRENT",), ("J SMITH",), at(D))
    assert len(t.self_transfers_as_revenue(self_ledger("5000.00", app=app), AS_OF)) == 1
    other = Application("a", None, (), ("A OTHER",), at(D))
    assert t.self_transfers_as_revenue(self_ledger("5000.00", app=other), AS_OF) == []


# ---------------------------------------------------------------- unexplained_recurring_debit


def debits(n: int, same: int, cp: str = "YRDEAL CAPITAL") -> Ledger:
    rows = [
        bank(
            f"d{i}",
            TODAY - timedelta(days=i + 1),
            "450.00" if i < same else f"{400 + i}.00",
            "out",
            cp,
        )
        for i in range(n)
    ]
    return Ledger(bank_txns=tuple(rows))


def test_recurring_debit_count_boundary() -> None:
    assert t.unexplained_recurring_debit(debits(9, 9), AS_OF) == []
    out = t.unexplained_recurring_debit(debits(10, 10), AS_OF)
    assert len(out) == 1 and out[0].severity == "review"
    assert out[0].facts["lender_like_name"] == "true"
    assert "lender" in out[0].summary


def test_recurring_debit_same_amount_share_boundary() -> None:
    assert t.unexplained_recurring_debit(debits(10, 7), AS_OF) == []
    out = t.unexplained_recurring_debit(debits(10, 8, "ACME SERVICES"), AS_OF)
    assert len(out) == 1 and "lender_like_name" not in out[0].facts


def test_recurring_debit_explained_by_payable_invoice() -> None:
    base = debits(12, 12)
    payable = invoice("i", D, cp="yrdeal capital")
    assert (
        t.unexplained_recurring_debit(Ledger(bank_txns=base.bank_txns, invoices=(payable,)), AS_OF)
        == []
    )
    receivable = invoice("i", D, kind="receivable", cp="YRDEAL CAPITAL")
    led = Ledger(bank_txns=base.bank_txns, invoices=(receivable,))
    assert len(t.unexplained_recurring_debit(led, AS_OF)) == 1


# ---------------------------------------------------------------- shared rules


def test_time_rule_and_determinism() -> None:
    txns = debits(12, 12).bank_txns
    hidden = tuple(
        BankTxn(**{**x.__dict__, "observed_at": AS_OF + timedelta(seconds=1)}) for x in txns
    )
    assert t.unexplained_recurring_debit(Ledger(bank_txns=hidden), AS_OF) == []
    a = t.unexplained_recurring_debit(Ledger(bank_txns=txns), AS_OF)
    b = t.unexplained_recurring_debit(Ledger(bank_txns=tuple(reversed(txns))), AS_OF)
    assert a == b and len(a) == 1


def test_evidence_capped_at_50() -> None:
    out = t.round_number_pattern(round_ledger(80, 0), AS_OF)
    assert len(out[0].evidence_refs) == t.MAX_EVIDENCE


# ---------------------------------------------------------------- synthetic plants


def run_all(led: Ledger) -> dict[str, FindingDraft]:
    last = max(x.posted_on for x in led.bank_txns)
    as_of = at(last + timedelta(days=1), 12)
    fns = (
        t.backdated_edits,
        t.round_number_pattern,
        t.self_transfers_as_revenue,
        t.unexplained_recurring_debit,
    )
    return {f.check_key: f for fn in fns for f in fn(led, as_of)}


def test_synthetic_clean_is_quiet(synthetic_ledger: Any) -> None:
    assert run_all(synthetic_ledger(days=200)) == {}


def test_synthetic_self_transfer_plant(synthetic_ledger: Any) -> None:
    found = run_all(synthetic_ledger(plant=("self_transfer_revenue",), days=200))
    assert found["self_transfers_as_revenue"].severity == "review"


def test_synthetic_lender_plant(synthetic_ledger: Any) -> None:
    found = run_all(synthetic_ledger(plant=("undisclosed_lender_debit",), days=200))
    d = found["unexplained_recurring_debit"]
    assert d.severity == "review" and d.facts["lender_like_name"] == "true"


@pytest.mark.parametrize("plant", ["hidden_reserve", "phantom_payouts", "partial_coverage"])
def test_synthetic_other_plants_never_hold(synthetic_ledger: Any, plant: str) -> None:
    found = run_all(synthetic_ledger(plant=(plant,), days=200))
    assert all(f.severity != "hold" for f in found.values())
