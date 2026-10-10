"""Ledger rows as metrics and checks see them, plus the one place the time rule lives."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from tangible.ledger.evidence import EvidenceRef


class Observed(Protocol):
    @property
    def observed_at(self) -> datetime: ...


class Revisable(Observed, Protocol):
    @property
    def source_ref(self) -> str: ...


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
    channel: str | None = None
    currency: str = "GBP"
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class Refund:
    source_ref: str
    order_ref: str
    amount: Decimal
    occurred_at: datetime
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class Payout:
    source_ref: str
    processor: str
    amount: Decimal
    fees: Decimal
    expected_on: date | None
    settled_on: date | None
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class BankTxn:
    """`amount` is always positive; `direction` gives the sign (see `signed`)."""

    source_ref: str
    account_ref: str
    amount: Decimal
    direction: Literal["in", "out"]
    counterparty: str | None
    description: str | None
    posted_on: date
    observed_at: datetime
    evidence: EvidenceRef | None = None

    @property
    def signed(self) -> Decimal:
        return self.amount if self.direction == "in" else -self.amount


@dataclass(frozen=True)
class Invoice:
    source_ref: str
    kind: Literal["payable", "receivable"]
    counterparty: str | None
    amount: Decimal
    issued_on: date
    due_on: date | None
    paid_on: date | None
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class InventorySnapshot:
    source_ref: str
    sku: str
    units_on_hand: Decimal
    unit_cost: Decimal | None
    as_of: date
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class AccountBalance:
    source_ref: str
    account_ref: str
    balance: Decimal
    as_of: date
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class Connection:
    source_ref: str
    source: str
    granted_at: datetime
    revoked_at: datetime | None
    last_success_at: datetime | None
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class Application:
    source_ref: str
    stated_revenue_90d: Decimal | None
    own_account_names: tuple[str, ...]
    owner_names: tuple[str, ...]
    observed_at: datetime
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class StatementLine:
    posted_on: date
    amount: Decimal  # positive; direction gives the sign
    direction: Literal["in", "out"]
    description: str


@dataclass(frozen=True)
class Statement:
    """Fields extracted from an uploaded bank statement (untrusted until reconciled).

    observed_at is when the extraction ran; evidence points at the uploaded file's pull.
    file_traits holds PDF metadata (producer, creation/modification dates, fonts) as strings.
    """

    source_ref: str
    account_ref: str
    period_start: date
    period_end: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    lines: tuple[StatementLine, ...]
    file_traits: tuple[tuple[str, str], ...]
    observed_at: datetime
    evidence: EvidenceRef | None = None


def _known[R: Revisable](rows: Iterable[R], as_of: datetime) -> tuple[R, ...]:
    known = known_as_of(rows, as_of, key=lambda r: r.source_ref)
    return tuple(sorted(known, key=lambda r: (r.observed_at, r.source_ref)))


@dataclass(frozen=True)
class Ledger:
    """A borrower's ledger rows. Metrics and checks take this plus an as_of."""

    orders: tuple[Order, ...] = ()
    refunds: tuple[Refund, ...] = ()
    payouts: tuple[Payout, ...] = ()
    bank_txns: tuple[BankTxn, ...] = ()
    invoices: tuple[Invoice, ...] = ()
    inventory_snapshots: tuple[InventorySnapshot, ...] = ()
    account_balances: tuple[AccountBalance, ...] = ()
    connections: tuple[Connection, ...] = ()
    applications: tuple[Application, ...] = ()
    statements: tuple[Statement, ...] = ()

    @property
    def application(self) -> Application | None:
        """The most recently observed application, or None."""
        if not self.applications:
            return None
        return max(self.applications, key=lambda a: (a.observed_at, a.source_ref))

    def known(self, as_of: datetime) -> "Ledger":
        """The single entry point for the time rule: every table through `known_as_of`.

        Output tuples are sorted by (observed_at, source_ref) so results are deterministic
        regardless of input order. Metrics and checks must call this before filtering on
        occurred_at or any other business date.
        """
        return Ledger(
            orders=_known(self.orders, as_of),
            refunds=_known(self.refunds, as_of),
            payouts=_known(self.payouts, as_of),
            bank_txns=_known(self.bank_txns, as_of),
            invoices=_known(self.invoices, as_of),
            inventory_snapshots=_known(self.inventory_snapshots, as_of),
            account_balances=_known(self.account_balances, as_of),
            connections=_known(self.connections, as_of),
            applications=_known(self.applications, as_of),
            statements=_known(self.statements, as_of),
        )
