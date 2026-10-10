"""WP1B health metrics: hand-computed values, None cases, and the time rule per metric."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.ledger.types import (
    AccountBalance,
    BankTxn,
    InventorySnapshot,
    Invoice,
    Ledger,
    Order,
    Payout,
    Refund,
)
from tangible.metrics import REGISTRY, compute

AS_OF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(hours=2)  # same business date as AS_OF, earlier knowledge
SEEN = AS_OF - timedelta(days=100)  # base rows were observed long ago
REVISED_AT = AS_OF - timedelta(hours=1)  # visible at AS_OF, not at BEFORE
LATE = AS_OF + timedelta(hours=1)  # observed after AS_OF: must be ignored


def at(days_ago: int) -> datetime:
    return AS_OF - timedelta(days=days_ago)


def on(days_ago: int) -> date:
    return AS_OF.date() - timedelta(days=days_ago)


def D(x: str | int) -> Decimal:
    return Decimal(x)


def order(
    ref: str,
    gross: str,
    *,
    days_ago: int = 10,
    disc: str = "0",
    cust: str | None = None,
    seen: datetime = SEEN,
) -> Order:
    return Order(ref, D(gross), D(disc), at(days_ago), seen, customer_ref=cust)


def refund(ref: str, amount: str, *, days_ago: int = 5, seen: datetime = SEEN) -> Refund:
    return Refund(ref, "o1", D(amount), at(days_ago), seen)


def snap(
    ref: str, sku: str, units: str, cost: str, days_ago: int, seen: datetime = SEEN
) -> InventorySnapshot:
    return InventorySnapshot(ref, sku, D(units), D(cost), on(days_ago), seen)


def payable(
    ref: str,
    amount: str,
    *,
    issued: int,
    due: int | None,
    paid: int | None = None,
    seen: datetime = SEEN,
    kind: str = "payable",
) -> Invoice:
    return Invoice(
        ref,
        kind,  # type: ignore[arg-type]
        "ACME",
        D(amount),
        on(issued),
        None if due is None else on(due),
        None if paid is None else on(paid),
        seen,
    )


def txn(ref: str, amount: str, direction: str, days_ago: int, seen: datetime = SEEN) -> BankTxn:
    return BankTxn(ref, "acc1", D(amount), direction, None, None, on(days_ago), seen)  # type: ignore[arg-type]


def balance(ref: str, acct: str, amount: str, days_ago: int, seen: datetime = SEEN):
    return AccountBalance(ref, acct, D(amount), on(days_ago), seen)


def payout(ref: str, *, settled: int | None, lag: int, seen: datetime = SEEN) -> Payout:
    settled_on = None if settled is None else on(settled)
    expected = None if settled is None else on(settled + lag)
    return Payout(ref, "stripe", D(100), D(3), expected, settled_on, seen)


# ---------------------------------------------------------------- gross_margin_est


def margin_ledger(**extra) -> Ledger:
    """Net revenue 1000. Opening stock 100x5=500 (window start), closing 80x5=400, one 30-day
    supplier invoice of 300 (purchase) and a 0-day opex invoice of 999 (not a purchase).
    COGS = 500 + 300 - 400 = 400; margin = 600 / 1000 = 0.6."""
    return Ledger(
        orders=(order("o1", "1000"),) + extra.get("orders", ()),
        inventory_snapshots=(
            snap("s0", "A", "100", "5", 90),
            snap("s1", "A", "80", "5", 1),
        )
        + extra.get("snaps", ()),
        invoices=(
            payable("p1", "300", issued=50, due=20),
            payable("p2", "999", issued=50, due=50),
        )
        + extra.get("invoices", ()),
    )


def test_gross_margin_value():
    assert compute("gross_margin_est", 1, margin_ledger(), AS_OF) == D("0.6")


def test_gross_margin_none():
    assert compute("gross_margin_est", 1, Ledger(), AS_OF) is None
    assert compute("gross_margin_est", 1, Ledger(orders=(order("o1", "10"),)), AS_OF) is None
    no_revenue = Ledger(
        inventory_snapshots=(snap("s0", "A", "1", "1", 90), snap("s1", "A", "1", "1", 1))
    )
    assert compute("gross_margin_est", 1, no_revenue, AS_OF) is None


def test_gross_margin_ignores_unobserved_rows():
    ledger = margin_ledger(
        orders=(order("late", "1000", seen=LATE),),
        invoices=(payable("late-p", "500", issued=5, due=-25, seen=LATE),),
    )
    assert compute("gross_margin_est", 1, ledger, AS_OF) == D("0.6")


def test_gross_margin_revision_applies_once_observed():
    revised = snap("s1", "A", "70", "5", 1, seen=REVISED_AT)  # closing 350 -> COGS 450 -> 0.55
    ledger = margin_ledger(snaps=(revised,))
    assert compute("gross_margin_est", 1, ledger, BEFORE) == D("0.6")
    assert compute("gross_margin_est", 1, ledger, AS_OF) == D("0.55")


# ---------------------------------------------------------------- repeat_customer_share


def repeat_ledger(**extra) -> Ledger:
    """c1: 75 + 60 = 135 (repeat). c2: 100 (single). anonymous: 65. Gross-disc = 300, refund 30,
    net revenue 270 -> 135 / 270 = 0.5."""
    return Ledger(
        orders=(
            order("o1", "85", disc="10", cust="c1"),
            order("o2", "60", cust="c1", days_ago=20),
            order("o3", "100", cust="c2"),
            order("o4", "65"),
        )
        + extra.get("orders", ()),
        refunds=(refund("r1", "30"),),
    )


def test_repeat_share_value():
    assert compute("repeat_customer_share", 1, repeat_ledger(), AS_OF) == D("0.5")


def test_repeat_share_none():
    assert compute("repeat_customer_share", 1, Ledger(), AS_OF) is None


def test_repeat_share_ignores_unobserved_rows():
    # A late second order would make c2 a repeat customer.
    ledger = repeat_ledger(orders=(order("late", "50", cust="c2", seen=LATE),))
    assert compute("repeat_customer_share", 1, ledger, AS_OF) == D("0.5")


def test_repeat_share_revision_applies_once_observed():
    revised = Order("o2", D(60), D(0), at(20), REVISED_AT, customer_ref="c3")  # c1 now single
    ledger = repeat_ledger(orders=(revised,))
    assert compute("repeat_customer_share", 1, ledger, BEFORE) == D("0.5")
    assert compute("repeat_customer_share", 1, ledger, AS_OF) == D(0)


# ---------------------------------------------------------------- days_of_inventory


def doi_ledger(**extra) -> Ledger:
    """Opening 100x5=500, purchases 310 (30-day terms), closing 72x5=360 -> COGS 450 = 5/day;
    360 / 5 = 72 days."""
    return Ledger(
        inventory_snapshots=(
            snap("s0", "A", "100", "5", 90),
            snap("s1", "A", "72", "5", 1),
        ),
        invoices=(payable("p1", "310", issued=50, due=20),) + extra.get("invoices", ()),
    )


def test_days_of_inventory_value():
    assert compute("days_of_inventory", 1, doi_ledger(), AS_OF) == D(72)


def test_days_of_inventory_none():
    assert compute("days_of_inventory", 1, Ledger(), AS_OF) is None
    # Stock grew with no purchases: COGS is negative -> not meaningful.
    growing = Ledger(
        inventory_snapshots=(snap("s0", "A", "100", "5", 90), snap("s1", "A", "120", "5", 1))
    )
    assert compute("days_of_inventory", 1, growing, AS_OF) is None


def test_days_of_inventory_ignores_unobserved_rows():
    ledger = doi_ledger(invoices=(payable("late", "500", issued=5, due=-25, seen=LATE),))
    assert compute("days_of_inventory", 1, ledger, AS_OF) == D(72)


def test_days_of_inventory_revision_applies_once_observed():
    revised = payable("p1", "400", issued=50, due=20, seen=REVISED_AT)  # COGS 540 = 6/day
    ledger = doi_ledger(invoices=(revised,))
    assert compute("days_of_inventory", 1, ledger, BEFORE) == D(72)
    assert compute("days_of_inventory", 1, ledger, AS_OF) == D(60)


# ---------------------------------------------------------------- aged_stock_share


def aged_ledger(**extra) -> Ledger:
    """A (cost 10): 10,10,10 never sold -> aged, closing 100. B (cost 4): 20,15,15 sold,
    closing 60. C (cost 8): one snapshot, not judged, closing 40. 100 / 200 = 0.5."""
    return Ledger(
        inventory_snapshots=(
            snap("a1", "A", "10", "10", 60),
            snap("a2", "A", "10", "10", 30),
            snap("a3", "A", "10", "10", 1),
            snap("b1", "B", "20", "4", 60),
            snap("b2", "B", "15", "4", 30),
            snap("b3", "B", "15", "4", 1),
            snap("c1", "C", "5", "8", 1),
        )
        + extra.get("snaps", ())
    )


def test_aged_stock_value():
    assert compute("aged_stock_share", 1, aged_ledger(), AS_OF) == D("0.5")


def test_aged_stock_none():
    assert compute("aged_stock_share", 1, Ledger(), AS_OF) is None
    single = Ledger(inventory_snapshots=(snap("c1", "C", "5", "8", 1),))
    assert compute("aged_stock_share", 1, single, AS_OF) is None


def test_aged_stock_ignores_unobserved_rows():
    ledger = aged_ledger(snaps=(snap("a4", "A", "5", "10", 0, seen=LATE),))  # a sale, unseen
    assert compute("aged_stock_share", 1, ledger, AS_OF) == D("0.5")


def test_aged_stock_revision_applies_once_observed():
    revised = snap("b1", "B", "10", "4", 60, seen=REVISED_AT)  # B 10,15,15: no sale -> aged
    ledger = aged_ledger(snaps=(revised,))
    assert compute("aged_stock_share", 1, ledger, BEFORE) == D("0.5")
    assert compute("aged_stock_share", 1, ledger, AS_OF) == D("0.8")  # 160 / 200


# ---------------------------------------------------------------- cash_runway


def cash_ledger(**extra) -> Ledger:
    """Balance: acc1 latest 3000 (older 5000 superseded) + acc2 1000 = 4000. Net flow in window
    = +1000 - 5000 - 2000 = -6000 (old out-of-window row ignored) -> 2000/month -> 2 months."""
    return Ledger(
        account_balances=(
            balance("b1", "acc1", "5000", 100),
            balance("b2", "acc1", "3000", 2),
            balance("b3", "acc2", "1000", 5),
        ),
        bank_txns=(
            txn("t1", "1000", "in", 20),
            txn("t2", "5000", "out", 30),
            txn("t3", "2000", "out", 40),
            txn("t0", "9999", "out", 100),
        )
        + extra.get("txns", ()),
    )


def test_cash_runway_value():
    assert compute("cash_runway", 1, cash_ledger(), AS_OF) == D(2)


def test_cash_runway_none_when_missing_or_not_burning():
    assert compute("cash_runway", 1, Ledger(), AS_OF) is None
    not_burning = Ledger(
        account_balances=(balance("b1", "acc1", "100", 1),),
        bank_txns=(txn("t1", "500", "in", 10), txn("t2", "400", "out", 10)),
    )
    assert compute("cash_runway", 1, not_burning, AS_OF) is None


def test_cash_runway_ignores_unobserved_rows():
    ledger = cash_ledger(txns=(txn("late", "30000", "out", 3, seen=LATE),))
    assert compute("cash_runway", 1, ledger, AS_OF) == D(2)


def test_cash_runway_revision_applies_once_observed():
    revised = txn("t2", "2000", "out", 30, seen=REVISED_AT)  # net -3000 -> 1000/month -> 4
    ledger = cash_ledger(txns=(revised,))
    assert compute("cash_runway", 1, ledger, BEFORE) == D(2)
    assert compute("cash_runway", 1, ledger, AS_OF) == D(4)


# ---------------------------------------------------------------- payables_discipline


def discipline_ledger(**extra) -> Ledger:
    """In population: a (paid early), b (paid late), c (unpaid past due = late), h (paid on due
    date). Excluded: d (due today, unpaid), e (due before window), f (receivable), g (credit
    note). 2 on time of 4 = 0.5."""
    return Ledger(
        invoices=(
            payable("a", "10", issued=60, due=30, paid=35),
            payable("b", "10", issued=60, due=30, paid=25),
            payable("c", "10", issued=40, due=20),
            payable("d", "10", issued=30, due=0),
            payable("e", "10", issued=150, due=100, paid=100),
            payable("f", "10", issued=60, due=30, paid=25, kind="receivable"),
            payable("g", "-10", issued=60, due=30, paid=25),
            payable("h", "10", issued=20, due=10, paid=10),
        )
        + extra.get("invoices", ())
    )


def test_payables_discipline_value():
    assert compute("payables_discipline", 1, discipline_ledger(), AS_OF) == D("0.5")


def test_payables_discipline_none():
    assert compute("payables_discipline", 1, Ledger(), AS_OF) is None


def test_payables_discipline_ignores_unobserved_rows():
    ledger = discipline_ledger(
        invoices=(payable("late", "10", issued=40, due=15, paid=16, seen=LATE),)
    )
    assert compute("payables_discipline", 1, ledger, AS_OF) == D("0.5")


def test_payables_discipline_revision_applies_once_observed():
    revised = payable("c", "10", issued=40, due=20, paid=21, seen=REVISED_AT)
    ledger = discipline_ledger(invoices=(revised,))
    assert compute("payables_discipline", 1, ledger, BEFORE) == D("0.5")
    assert compute("payables_discipline", 1, ledger, AS_OF) == D("0.75")


# ---------------------------------------------------------------- payout_lag


def lag_ledger(**extra) -> Ledger:
    """Lags 2, 4, 3 settled in window -> median 3. Ignored: settled before window, unsettled."""
    return Ledger(
        payouts=(
            payout("p1", settled=10, lag=2),
            payout("p2", settled=20, lag=4),
            payout("p3", settled=30, lag=3),
            payout("old", settled=100, lag=9),
            payout("open", settled=None, lag=9),
        )
        + extra.get("payouts", ())
    )


def test_payout_lag_value_odd_and_even():
    assert compute("payout_lag", 1, lag_ledger(), AS_OF) == D(3)
    even = lag_ledger(payouts=(payout("p4", settled=40, lag=5),))  # 2,3,4,5 -> 3.5
    assert compute("payout_lag", 1, even, AS_OF) == D("3.5")


def test_payout_lag_none():
    assert compute("payout_lag", 1, Ledger(), AS_OF) is None


def test_payout_lag_ignores_unobserved_rows():
    ledger = lag_ledger(payouts=(payout("late", settled=5, lag=30, seen=LATE),))
    assert compute("payout_lag", 1, ledger, AS_OF) == D(3)


def test_payout_lag_revision_applies_once_observed():
    revised = payout("p1", settled=10, lag=8, seen=REVISED_AT)  # 8,4,3 -> median 4
    ledger = lag_ledger(payouts=(revised,))
    assert compute("payout_lag", 1, ledger, BEFORE) == D(3)
    assert compute("payout_lag", 1, ledger, AS_OF) == D(4)


# ---------------------------------------------------------------- history_days


def history_ledger(**extra) -> Ledger:
    """Earliest known order 200 days before as_of; later order does not matter."""
    return Ledger(
        orders=(order("o1", "5", days_ago=200), order("o2", "5", days_ago=10))
        + extra.get("orders", ())
    )


def test_history_days_value():
    assert compute("history_days", 1, history_ledger(), AS_OF) == D(200)


def test_history_days_none():
    assert compute("history_days", 1, Ledger(), AS_OF) is None


def test_history_days_ignores_unobserved_rows():
    ledger = history_ledger(orders=(order("early", "5", days_ago=300, seen=LATE),))
    assert compute("history_days", 1, ledger, AS_OF) == D(200)


def test_history_days_revision_applies_once_observed():
    revised = Order("o1", D(5), D(0), at(250), REVISED_AT)
    ledger = history_ledger(orders=(revised,))
    assert compute("history_days", 1, ledger, BEFORE) == D(199)  # 199d 22h, floored
    assert compute("history_days", 1, ledger, AS_OF) == D(250)


# ---------------------------------------------------------------- payables_days


def pdays_ledger(**extra) -> Ledger:
    """Paid in window: 30 days and 20 days -> 25. Ignored: paid before window, unpaid, credit."""
    return Ledger(
        invoices=(
            payable("a", "10", issued=60, due=40, paid=30),
            payable("b", "10", issued=40, due=25, paid=20),
            payable("old", "10", issued=150, due=120, paid=100),
            payable("open", "10", issued=40, due=25),
            payable("cn", "-10", issued=60, due=40, paid=10),
        )
        + extra.get("invoices", ())
    )


def test_payables_days_value():
    assert compute("payables_days", 1, pdays_ledger(), AS_OF) == D(25)


def test_payables_days_none():
    assert compute("payables_days", 1, Ledger(), AS_OF) is None


def test_payables_days_ignores_unobserved_rows():
    ledger = pdays_ledger(invoices=(payable("late", "10", issued=90, due=60, paid=5, seen=LATE),))
    assert compute("payables_days", 1, ledger, AS_OF) == D(25)


def test_payables_days_revision_applies_once_observed():
    revised = payable("a", "10", issued=60, due=40, paid=10, seen=REVISED_AT)  # 50 and 20 -> 35
    ledger = pdays_ledger(invoices=(revised,))
    assert compute("payables_days", 1, ledger, BEFORE) == D(25)
    assert compute("payables_days", 1, ledger, AS_OF) == D(35)


# ---------------------------------------------------------------- synthetic ledger

NEW_KEYS = [
    "gross_margin_est",
    "repeat_customer_share",
    "days_of_inventory",
    "aged_stock_share",
    "cash_runway",
    "payables_discipline",
    "payout_lag",
    "history_days",
    "payables_days",
]

# key -> (lo, hi) plausible range on the healthy synthetic borrower; None => value optional.
RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "gross_margin_est": (D("0.2"), D("0.7")),
    "repeat_customer_share": (D(0), D(1)),
    "days_of_inventory": (D(1), D(365)),
    "aged_stock_share": (D(0), D(1)),
    "payables_discipline": (D("0.7"), D(1)),
    "payout_lag": (D(0), D(7)),
    "history_days": (D(150), D(250)),
    "payables_days": (D(0), D(60)),
}


@pytest.fixture(scope="module")
def synth(synthetic_ledger: Callable[..., Ledger]) -> tuple[Ledger, datetime]:
    ledger = synthetic_ledger(days=200)
    last = max(t.posted_on for t in ledger.bank_txns)
    return ledger, datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def test_all_metrics_registered():
    for key in NEW_KEYS:
        assert (key, 1) in REGISTRY


def test_synthetic_values_plausible(synth):
    ledger, as_of = synth
    values = {key: compute(key, 1, ledger, as_of) for key in NEW_KEYS}
    print("\nsynthetic as_of", as_of.date())
    for key, v in values.items():
        print(f"  {key:22} {v}")
    for key, (lo, hi) in RANGES.items():
        v = values[key]
        assert v is not None, key
        assert lo <= v <= hi, (key, v)
    # cash_runway is None when the synthetic business is not burning cash (policy: "not
    # evaluated"); when it is burning it must be a positive number of months.
    runway = values["cash_runway"]
    assert runway is None or runway > 0


def test_synthetic_idempotent(synth):
    ledger, as_of = synth
    for key in NEW_KEYS:
        assert compute(key, 1, ledger, as_of) == compute(key, 1, ledger, as_of)
        assert compute(key, 1, ledger, as_of) is None or isinstance(
            compute(key, 1, ledger, as_of), Decimal
        )
