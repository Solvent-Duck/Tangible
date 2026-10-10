from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import tangible.metrics  # noqa: F401
from tangible.ledger.types import Ledger, Order, Refund, known_as_of
from tangible.metrics import REGISTRY, compute, metric

AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def order(ref: str, gross: str, *, days_ago: int = 10, seen_days_ago: int = 9, disc: str = "0"):
    return Order(
        source_ref=ref,
        gross=Decimal(gross),
        discounts=Decimal(disc),
        occurred_at=AS_OF - timedelta(days=days_ago),
        observed_at=AS_OF - timedelta(days=seen_days_ago),
    )


def refund(ref: str, amount: str, *, seen_days_ago: int = 5):
    return Refund(
        source_ref=ref,
        order_ref="o1",
        amount=Decimal(amount),
        occurred_at=AS_OF - timedelta(days=6),
        observed_at=AS_OF - timedelta(days=seen_days_ago),
    )


def test_net_revenue_and_refund_rate():
    ledger = Ledger(
        orders=(order("o1", "100", disc="10"), order("o2", "100")),
        refunds=(refund("r1", "20"),),
    )
    assert compute("net_revenue", 1, ledger, AS_OF) == Decimal(170)
    assert compute("refund_rate", 1, ledger, AS_OF) == Decimal("0.1")


def test_refund_rate_none_without_orders():
    assert compute("refund_rate", 1, Ledger(), AS_OF) is None


def test_orders_outside_90_day_window_excluded():
    ledger = Ledger(orders=(order("old", "500", days_ago=91), order("new", "100")))
    assert compute("net_revenue", 1, ledger, AS_OF) == Decimal(100)


def test_rows_observed_after_as_of_are_invisible():
    late = order("late", "999", seen_days_ago=-1)  # observed a day after as_of
    ledger = Ledger(orders=(order("o1", "100"), late))
    assert compute("net_revenue", 1, ledger, AS_OF) == Decimal(100)


def test_revision_supersedes_earlier_row_only_once_observed():
    first = order("o1", "100", seen_days_ago=20)
    revised = order("o1", "80", seen_days_ago=2)
    assert [o.gross for o in known_as_of([first, revised], AS_OF, lambda o: o.source_ref)] == [
        Decimal(80)
    ]
    before_revision = AS_OF - timedelta(days=10)
    assert [
        o.gross for o in known_as_of([first, revised], before_revision, lambda o: o.source_ref)
    ] == [Decimal(100)]


def test_duplicate_metric_version_rejected():
    with pytest.raises(ValueError):
        metric("net_revenue", 1)(lambda ledger, as_of: None)
    assert ("net_revenue", 1) in REGISTRY
