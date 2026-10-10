from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tangible.checks.registry import REGISTRY, FindingDraft, run_check
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import (
    Application,
    BankTxn,
    InventorySnapshot,
    Invoice,
    Ledger,
    Order,
    Payout,
    Refund,
)

D = Decimal
AS_OF = datetime(2024, 6, 30, 6, tzinfo=UTC)  # window: 2024-04-01 <= d < 2024-06-30
CHECKS = (
    "payouts_vs_deposits",
    "books_vs_processor",
    "orders_vs_payouts",
    "inventory_flow",
    "payables_vs_bank",
    "stated_vs_observed",
)
_n = 0


def ev() -> EvidenceRef:
    global _n
    _n += 1
    return EvidenceRef("pull", f"row={_n}")


def at(d: date, hour: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def run(key: str, ledger: Ledger, as_of: datetime = AS_OF) -> list[FindingDraft]:
    return run_check(key, 1, ledger, as_of)


def payout(ref: str, amount: str, settled: date, *, expected: date | None = None, fees: str = "0"):
    return Payout(ref, "shopify", D(amount), D(fees), expected, settled, at(settled), evidence=ev())


def deposit(ref: str, amount: str, posted: date, *, observed: datetime | None = None, d="in"):
    return BankTxn(
        ref, "ACC", D(amount), d, "SHOPIFY PAYMENTS", None, posted,
        observed or at(posted + timedelta(days=1), 6), evidence=ev(),
    )  # fmt: skip


def order(ref: str, gross: str, on: date, discounts: str = "0") -> Order:
    return Order(ref, D(gross), D(discounts), at(on, 10), at(on, 10))


def invoice(ref, kind, amount, issued, *, paid=None, cp="SUP", terms=30) -> Invoice:
    return Invoice(
        ref, kind, cp, D(amount), issued, issued + timedelta(days=terms), paid,
        at(issued), evidence=ev(),
    )  # fmt: skip


# registry


def test_all_registered_as_integrity_v1():
    for key in CHECKS:
        assert (key, 1) in REGISTRY


def test_findings_carry_pillar_and_key():
    ledger = Ledger(
        payouts=(payout("P1", "100", date(2024, 5, 1)),),
        bank_txns=(deposit("B1", "50", date(2024, 5, 1)),),
    )
    (f,) = run("payouts_vs_deposits", ledger)
    assert f.pillar == "integrity" and f.check_key == "payouts_vs_deposits" and f.check_version == 1


# payouts vs deposits


def _payouts_ledger(n_payouts: int, n_unmatched: int) -> Ledger:
    """n payouts of 100 over April-May; the first n_unmatched have no deposit."""
    payouts, deposits = [], []
    for i in range(n_payouts):
        day = date(2024, 4, 2) + timedelta(days=i)
        payouts.append(payout(f"P{i}", "100", day))
        if i >= n_unmatched:
            deposits.append(deposit(f"B{i}", "100", day + timedelta(days=1)))
    return Ledger(payouts=tuple(payouts), bank_txns=tuple(deposits))


@pytest.mark.parametrize(
    ("unmatched", "severity"),
    [(0, None), (1, None), (2, "review"), (3, "review"), (4, "hold")],
)
def test_payouts_vs_deposits_boundaries(unmatched, severity):
    # 20 payouts: 1 unmatched = exactly 95% (no finding), 3 = exactly 85% (review, not hold)
    out = run("payouts_vs_deposits", _payouts_ledger(20, unmatched))
    if severity is None:
        assert out == []
        return
    (f,) = out
    assert f.severity == severity
    assert f.facts["unmatched_payouts"] == str(unmatched)
    assert f.facts["matched_share"] == f"{D(20 - unmatched) / 20:.4f}"
    assert len(f.evidence_refs) == unmatched
    assert "%" in f.summary


def test_payouts_vs_deposits_tolerances():
    day = date(2024, 5, 10)
    p = (payout("P1", "1000", day),)
    ok = [
        deposit("B", "1010", day + timedelta(days=3)),  # +1% and +3 days: on the edge
    ]
    assert run("payouts_vs_deposits", Ledger(payouts=p, bank_txns=tuple(ok))) == []
    for amount, offset in (("1010.01", 0), ("990", 4), ("989.99", 0), ("1000", -4)):
        bad = deposit("B", amount, day + timedelta(days=offset))
        out = run("payouts_vs_deposits", Ledger(payouts=p, bank_txns=(bad,)))
        assert out and out[0].severity == "hold", (amount, offset)


def test_payouts_vs_deposits_one_to_one():
    day = date(2024, 5, 10)
    payouts = (payout("P1", "500", day), payout("P2", "500", day))
    one_deposit = (deposit("B1", "500", day),)
    (f,) = run("payouts_vs_deposits", Ledger(payouts=payouts, bank_txns=one_deposit))
    assert f.facts["matched_share"] == "0.5000"
    two = one_deposit + (deposit("B2", "500", day),)
    assert run("payouts_vs_deposits", Ledger(payouts=payouts, bank_txns=two)) == []


def test_payouts_vs_deposits_ignores_outflows_and_unsettled():
    day = date(2024, 5, 10)
    pending = replace(payout("P2", "999", day), settled_on=None)
    ledger = Ledger(
        payouts=(payout("P1", "500", day), pending),
        bank_txns=(deposit("B1", "500", day), deposit("B2", "500", day, d="out")),
    )
    assert run("payouts_vs_deposits", ledger) == []


def test_payouts_vs_deposits_no_payouts_is_empty():
    assert (
        run("payouts_vs_deposits", Ledger(bank_txns=(deposit("B", "5", date(2024, 5, 1)),))) == []
    )


def test_payouts_vs_deposits_time_rule():
    day = date(2024, 5, 10)
    payouts = (payout("P1", "500", day),)
    seen = deposit("B1", "500", day)
    assert run("payouts_vs_deposits", Ledger(payouts=payouts, bank_txns=(seen,))) == []
    late = deposit("B1", "500", day, observed=AS_OF + timedelta(hours=1))
    (f,) = run("payouts_vs_deposits", Ledger(payouts=payouts, bank_txns=(late,)))
    assert f.severity == "hold" and f.facts["matched_share"] == "0.0000"
    # Observed later, but still before an as_of that is late enough: matched.
    assert (
        run(
            "payouts_vs_deposits",
            Ledger(payouts=payouts, bank_txns=(late,)),
            AS_OF + timedelta(days=1),
        )
        == []
    )


def test_payouts_vs_deposits_immature_payouts_not_judged():
    # Settled two days before as_of: its deposit could not have been seen yet.
    p = payout("P1", "500", date(2024, 6, 27))
    assert run("payouts_vs_deposits", Ledger(payouts=(p,))) == []


def test_evidence_capped_at_fifty_most_material_first():
    payouts = tuple(
        payout(f"P{i}", str(100 + i), date(2024, 4, 2) + timedelta(days=i // 2)) for i in range(70)
    )
    (f,) = run("payouts_vs_deposits", Ledger(payouts=payouts))
    assert len(f.evidence_refs) == 50
    biggest = next(p for p in payouts if p.source_ref == "P69")
    assert f.evidence_refs[0] == str(biggest.evidence)


# books vs processor


def _books_ledger(processor_total: int) -> Ledger:
    """May books: one card order 100 (payout 90 + 10 fees) and one wholesale order 1000."""
    d = date(2024, 5, 10)
    return Ledger(
        orders=(order("O1", "100", d), order("O2", "1000", d)),
        payouts=(payout("P1", "90", date(2024, 5, 13), expected=date(2024, 5, 11), fees="10"),),
        invoices=(invoice("INV-O2", "receivable", processor_total - 100, d),),
    )


@pytest.mark.parametrize(
    ("processor", "severity"),
    [(1100, None), (1045, None), (1040, "review"), (935, "review"), (930, "hold"), (1300, "hold")],
)
def test_books_vs_processor_boundaries(processor, severity):
    out = run("books_vs_processor", _books_ledger(processor))
    if severity is None:
        assert out == []
        return
    (f,) = out
    assert f.severity == severity
    assert f.facts["month"] == "2024-05"
    assert f.facts["books_net"] == "1100.00"
    assert f.facts["processor_total"] == f"{processor:.2f}"
    assert f.evidence_refs


def test_books_vs_processor_credit_notes_and_refunds_cancel():
    d = date(2024, 5, 10)
    ledger = replace(
        _books_ledger(1100),
        refunds=(Refund("R1", "R1", D("200"), at(d, 12), at(d, 12)),),
        invoices=(
            invoice("INV-O2", "receivable", "1000", d),
            invoice("CN-R1", "receivable", "-200", d),
        ),
    )
    # books = 1100 - 200 = 900; processor = 100 + 1000 - 200 = 900
    assert run("books_vs_processor", ledger) == []


def test_books_vs_processor_skips_partial_edge_months():
    # A huge mismatch in June (still running at as_of) and nothing judged in April: no finding.
    d = date(2024, 6, 10)
    ledger = Ledger(
        orders=(order("O1", "5000", d),),
        payouts=(payout("P1", "10", date(2024, 6, 12), expected=date(2024, 6, 11)),),
    )
    assert run("books_vs_processor", ledger) == []


def test_books_vs_processor_absent_sources():
    d = date(2024, 5, 10)
    assert run("books_vs_processor", Ledger(orders=(order("O1", "100", d),))) == []
    assert run("books_vs_processor", Ledger(payouts=(payout("P", "1", d),))) == []


def test_books_vs_processor_uses_worst_month():
    apr, may = date(2024, 4, 10), date(2024, 5, 10)
    ledger = Ledger(
        orders=(order("O1", "1000", apr), order("O2", "1000", may)),
        payouts=(payout("P1", "1", may, expected=may + timedelta(days=1)),),
        invoices=(
            invoice("I1", "receivable", "1000", apr),
            invoice("I2", "receivable", "1500", may),  # 50% too high in May
        ),
    )
    (f,) = run("books_vs_processor", ledger)
    assert f.severity == "hold" and f.facts["month"] == "2024-05"


# orders vs payouts


def _orders_ledger(order_gross: str, extra_orders=()) -> Ledger:
    """Payouts expected Apr 10 and Apr 17 (the latter covers Apr 10-16, paid 94 + 6 fees)."""
    return Ledger(
        orders=(order("O1", order_gross, date(2024, 4, 12)), *extra_orders),
        payouts=(
            payout("P0", "50", date(2024, 4, 12), expected=date(2024, 4, 10)),
            payout("P1", "94", date(2024, 4, 19), expected=date(2024, 4, 17), fees="6"),
        ),
    )


@pytest.mark.parametrize(
    ("gross", "flag"), [("100", False), ("105", False), ("106", True), ("90", True)]
)
def test_orders_vs_payouts_boundaries(gross, flag):
    out = run("orders_vs_payouts", _orders_ledger(gross))
    if not flag:
        assert out == []
        return
    (f,) = out
    assert f.severity == "review"
    assert f.facts["payouts_plus_fees"] == "100.00"
    assert f.facts["card_order_net"] == f"{D(gross):.2f}"
    assert f.evidence_refs


def test_orders_vs_payouts_ignores_wholesale_and_out_of_range_orders():
    extra = (
        order("W", "600", date(2024, 4, 12)),  # wholesale: not a card order
        order("EARLY", "999", date(2024, 4, 9)),
        order("LATE", "40", date(2024, 4, 17)),  # covered by the next payout, not this one
    )
    assert run("orders_vs_payouts", _orders_ledger("100", extra)) == []


def test_orders_vs_payouts_card_refunds_reduce_expected():
    ledger = replace(
        _orders_ledger("150"),
        refunds=(Refund("R1", "R1", D("50"), at(date(2024, 4, 14)), at(date(2024, 4, 14))),),
    )
    assert run("orders_vs_payouts", ledger) == []


def test_orders_vs_payouts_absent_payouts_is_empty():
    assert run("orders_vs_payouts", Ledger(orders=(order("O", "100", date(2024, 4, 12)),))) == []


# inventory flow


def snap(sku: str, units: str, on: date) -> InventorySnapshot:
    return InventorySnapshot(f"S-{sku}-{on}", sku, D(units), D("2"), on, at(on, 6), evidence=ev())


def test_inventory_flow_clean():
    snaps = tuple(
        snap("A", u, date(2024, 5, 6) + timedelta(days=7 * i))
        for i, u in enumerate("100 90 120".split())
    )
    assert run("inventory_flow", Ledger(inventory_snapshots=snaps)) == []


def test_inventory_flow_negative_units():
    snaps = (snap("A", "10", date(2024, 5, 6)), snap("A", "-3", date(2024, 5, 13)))
    (f,) = run("inventory_flow", Ledger(inventory_snapshots=snaps))
    assert f.severity == "review" and f.facts["negative_snapshots"] == "1" and f.evidence_refs


def test_inventory_flow_jump_needs_purchase_invoice():
    snaps = (snap("A", "100", date(2024, 5, 6)), snap("A", "5000", date(2024, 5, 13)))
    (f,) = run("inventory_flow", Ledger(inventory_snapshots=snaps))
    assert f.facts["unexplained_jumps"] == "1" and f.evidence_refs
    purchase = invoice("PUR1", "payable", "9000", date(2024, 5, 20))  # net 30, the Monday after
    assert run("inventory_flow", Ledger(inventory_snapshots=snaps, invoices=(purchase,))) == []
    # Short-terms payables (utilities, fees) and receivables are not stock purchases.
    short = invoice("UTL", "payable", "90", date(2024, 5, 14), terms=7)
    recv = invoice("REC", "receivable", "90", date(2024, 5, 14))
    ledger = Ledger(inventory_snapshots=snaps, invoices=(short, recv))
    assert run("inventory_flow", ledger)


def test_inventory_flow_small_or_recent_jumps_not_flagged():
    small = (snap("A", "1", date(2024, 5, 6)), snap("A", "40", date(2024, 5, 13)))  # +39 units
    assert run("inventory_flow", Ledger(inventory_snapshots=small)) == []
    recent = (snap("A", "100", date(2024, 6, 17)), snap("A", "5000", date(2024, 6, 24)))
    assert run("inventory_flow", Ledger(inventory_snapshots=recent)) == []  # invoice not due yet


def test_inventory_flow_absent_is_empty():
    assert run("inventory_flow", Ledger()) == []


# payables vs bank


def _paid_payable(amount: str = "500", paid: date = date(2024, 5, 10), cp: str = "SUP"):
    return invoice("PAY1", "payable", amount, paid - timedelta(days=30), paid=paid, cp=cp)


def _outflow(amount: str, posted: date, cp: str | None = "SUP") -> BankTxn:
    return replace(deposit("OUT1", amount, posted, d="out"), counterparty=cp)


def test_payables_vs_bank_clean_with_tolerances():
    paid = date(2024, 5, 10)
    for amount, offset, cp in (("500", 0, "SUP"), ("505", 3, "sup"), ("495", -3, "SUP ")):
        ledger = Ledger(
            invoices=(_paid_payable(),),
            bank_txns=(_outflow(amount, paid + timedelta(days=offset), cp),),
        )
        assert run("payables_vs_bank", ledger) == [], (amount, offset, cp)


@pytest.mark.parametrize(
    ("amount", "severity"),
    [("500", "info"), ("999.99", "info"), ("1000", "review"), ("25000", "review")],
)
def test_payables_vs_bank_missing_severity(amount, severity):
    other = _outflow("1", date(2024, 5, 1), "SOMEONE ELSE")
    ledger = Ledger(invoices=(_paid_payable(amount),), bank_txns=(other,))
    (f,) = run("payables_vs_bank", ledger)
    assert f.severity == severity
    assert f.facts["missing_total"] == f"{D(amount):.2f}"
    assert f.evidence_refs


def test_payables_vs_bank_total_of_small_misses_reaches_review():
    invs = tuple(
        replace(_paid_payable("600"), source_ref=f"PAY{i}", evidence=ev()) for i in range(2)
    )
    other = _outflow("1", date(2024, 5, 1), "SOMEONE ELSE")
    (f,) = run("payables_vs_bank", Ledger(invoices=invs, bank_txns=(other,)))
    assert f.severity == "review" and f.facts["missing_count"] == "2"


@pytest.mark.parametrize(
    ("amount", "offset", "cp"),
    [("489", 0, "SUP"), ("511", 0, "SUP"), ("500", 4, "SUP"), ("500", 0, "OTHER")],
)
def test_payables_vs_bank_mismatches(amount, offset, cp):
    paid = date(2024, 5, 10)
    ledger = Ledger(
        invoices=(_paid_payable(),),
        bank_txns=(_outflow(amount, paid + timedelta(days=offset), cp),),
    )
    (f,) = run("payables_vs_bank", ledger)
    assert f.severity == "info"


def test_payables_vs_bank_one_to_one_and_inflows_dont_count():
    paid = date(2024, 5, 10)
    twin = replace(_paid_payable(), source_ref="PAY2", evidence=ev())
    one_out = _outflow("500", paid)
    (f,) = run("payables_vs_bank", Ledger(invoices=(_paid_payable(), twin), bank_txns=(one_out,)))
    assert f.facts["missing_count"] == "1"
    inflow = deposit("IN1", "500", paid)
    assert run("payables_vs_bank", Ledger(invoices=(_paid_payable(),), bank_txns=(inflow,)))


def test_payables_vs_bank_unpaid_and_absent():
    unpaid = invoice("PAY9", "payable", "5000", date(2024, 5, 1))
    assert (
        run(
            "payables_vs_bank",
            Ledger(invoices=(unpaid,), bank_txns=(_outflow("1", date(2024, 5, 1)),)),
        )
        == []
    )
    assert run("payables_vs_bank", Ledger(invoices=(_paid_payable(),))) == []


# stated vs observed


def _stated_ledger(stated: str | None) -> Ledger:
    orders = (order("O1", "600", date(2024, 5, 1)), order("O2", "450", date(2024, 6, 1)))
    refunds = (Refund("R1", "R1", D("50"), at(date(2024, 6, 2)), at(date(2024, 6, 2))),)
    apps = ()
    if stated is not None:
        apps = (Application("APP", D(stated), (), (), at(date(2024, 4, 1)), evidence=ev()),)
    return Ledger(orders=orders, refunds=refunds, applications=apps)  # observed 1000


@pytest.mark.parametrize(
    ("stated", "flag"),
    [("1000", False), ("500", False), ("1100", False), ("1101", True), ("5000", True)],
)
def test_stated_vs_observed_boundaries(stated, flag):
    out = run("stated_vs_observed", _stated_ledger(stated))
    if not flag:
        assert out == []
        return
    (f,) = out
    assert f.severity == "review"
    assert f.facts["observed_revenue_90d"] == "1000.00"
    assert f.facts["stated_revenue_90d"] == f"{D(stated):.2f}"
    assert f.evidence_refs


def test_stated_vs_observed_no_application_or_no_figure():
    assert run("stated_vs_observed", _stated_ledger(None)) == []
    base = _stated_ledger("9999")
    no_figure = replace(base.applications[0], stated_revenue_90d=None)
    assert run("stated_vs_observed", replace(base, applications=(no_figure,))) == []


def test_stated_vs_observed_application_must_be_known():
    base = _stated_ledger("9999")
    late = replace(base.applications[0], observed_at=AS_OF + timedelta(days=1))
    assert run("stated_vs_observed", replace(base, applications=(late,))) == []


def test_stated_vs_observed_no_observed_revenue():
    app = Application("APP", D("500"), (), (), at(date(2024, 4, 1)), evidence=ev())
    (f,) = run("stated_vs_observed", Ledger(applications=(app,)))
    assert f.severity == "review" and f.evidence_refs


# determinism


def test_deterministic_and_input_order_independent():
    ledger = _payouts_ledger(20, 4)
    shuffled = Ledger(payouts=ledger.payouts[::-1], bank_txns=ledger.bank_txns[::-1])
    for key in CHECKS:
        assert run(key, ledger) == run(key, ledger)
    assert run("payouts_vs_deposits", ledger) == run("payouts_vs_deposits", shuffled)


# plant matrix


def as_of_for(ledger: Ledger) -> datetime:
    last = max(t.posted_on for t in ledger.bank_txns)
    return at(last + timedelta(days=1), 6)


def run_all(ledger: Ledger) -> dict[str, list[FindingDraft]]:
    as_of = as_of_for(ledger)
    return {key: run(key, ledger, as_of) for key in CHECKS}


def test_clean_synthetic_raises_nothing(synthetic_ledger):
    results = run_all(synthetic_ledger(plant=(), days=200))
    assert {k: v for k, v in results.items() if v} == {}


def test_clean_synthetic_quiet_at_every_week(synthetic_ledger):
    ledger = synthetic_ledger(plant=(), days=200)
    end = as_of_for(ledger)
    for weeks in range(0, 8):
        as_of = end - timedelta(days=7 * weeks)
        for key in CHECKS:
            assert [f for f in run(key, ledger, as_of) if f.severity != "info"] == [], (key, as_of)


def test_hidden_reserve_holds_payouts_vs_deposits(synthetic_ledger):
    results = run_all(synthetic_ledger(plant={"hidden_reserve"}, days=200))
    (f,) = results["payouts_vs_deposits"]
    assert f.severity == "hold"
    assert D(f.facts["matched_share"]) < D("0.85")
    assert f.evidence_refs


def test_phantom_payouts_flags_orders_vs_payouts(synthetic_ledger):
    results = run_all(synthetic_ledger(plant={"phantom_payouts"}, days=200))
    (f,) = results["orders_vs_payouts"]
    assert f.severity == "review"
    assert D(f.facts["gap"]) > D("0.05")
    assert D(f.facts["payouts_plus_fees"]) > D(f.facts["card_order_net"])


@pytest.mark.parametrize(
    "plant", ["undisclosed_lender_debit", "self_transfer_revenue", "partial_coverage"]
)
def test_other_plants_never_hold_here(synthetic_ledger, plant):
    results = run_all(synthetic_ledger(plant={plant}, days=200))
    assert [f for fs in results.values() for f in fs if f.severity == "hold"] == []
