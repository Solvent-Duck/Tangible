"""Ledger rows as metrics and checks see them, plus the one place the time rule lives."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


class Observed(Protocol):
    @property
    def observed_at(self) -> datetime: ...


def known_as_of[R: Observed](
    rows: Iterable[R], as_of: datetime, key: Callable[[R], str]
) -> list[R]:
    """Rows as the lender could have known them at `as_of`.

    Drops rows observed after `as_of`; when a source revised a record (same key, later
    observed_at), the latest revision known at `as_of` wins.
    """
    latest: dict[str, R] = {}
    for row in sorted(rows, key=lambda r: r.observed_at):
        if row.observed_at <= as_of:
            latest[key(row)] = row
    return list(latest.values())


@dataclass(frozen=True)
class Order:
    source_ref: str
    gross: Decimal
    discounts: Decimal
    occurred_at: datetime
    observed_at: datetime
    customer_ref: str | None = None


@dataclass(frozen=True)
class Refund:
    source_ref: str
    order_ref: str
    amount: Decimal
    occurred_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class Ledger:
    """A borrower's ledger rows. Metrics take this plus an as_of; extend as metrics need tables."""

    orders: tuple[Order, ...] = ()
    refunds: tuple[Refund, ...] = ()
