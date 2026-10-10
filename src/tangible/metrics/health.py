"""Health metrics (trailing 90 days unless noted)."""

from datetime import datetime, timedelta
from decimal import Decimal

from tangible.ledger.types import Ledger, Order, Refund, known_as_of
from tangible.metrics.registry import metric

WINDOW = timedelta(days=90)


def _window(ledger: Ledger, as_of: datetime) -> tuple[list[Order], list[Refund]]:
    start = as_of - WINDOW
    orders = [
        o
        for o in known_as_of(ledger.orders, as_of, lambda o: o.source_ref)
        if start < o.occurred_at <= as_of
    ]
    refunds = [
        r
        for r in known_as_of(ledger.refunds, as_of, lambda r: r.source_ref)
        if start < r.occurred_at <= as_of
    ]
    return orders, refunds


@metric("net_revenue", 1)
def net_revenue(ledger: Ledger, as_of: datetime) -> Decimal | None:
    orders, refunds = _window(ledger, as_of)
    return sum((o.gross - o.discounts for o in orders), Decimal(0)) - sum(
        (r.amount for r in refunds), Decimal(0)
    )


@metric("refund_rate", 1)
def refund_rate(ledger: Ledger, as_of: datetime) -> Decimal | None:
    orders, refunds = _window(ledger, as_of)
    gross = sum((o.gross for o in orders), Decimal(0))
    if gross == 0:
        return None
    return sum((r.amount for r in refunds), Decimal(0)) / gross
