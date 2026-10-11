"""Ledger rows as metrics and checks see them, plus the one place the time rule lives."""

import json
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
    """The loan application. The identity fields (legal_name .. naics, requested_amount) are the
    v2 row shape; rows written before it read them as None. US addresses: `address` is the street
    line plus city, `state` the two-letter code."""

    source_ref: str
    stated_revenue_90d: Decimal | None
    own_account_names: tuple[str, ...]
    owner_names: tuple[str, ...]
    observed_at: datetime
    evidence: EvidenceRef | None = None
    legal_name: str | None = None
    dba: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    state: str | None = None
    naics: str | None = None
    requested_amount: Decimal | None = None
    # v3: stated imports (systemic-risk plan). None = not stated; () = stated "no imports".
    import_mix: "tuple[ImportLine, ...] | None" = None


@dataclass(frozen=True)
class ImportLine:
    """One line of the applicant's stated import mix: goods of an HS chapter from a country, as a
    share of cost of goods sold. Self-reported (soft) in v1."""

    hs_chapter: str  # two digits, e.g. "94"
    country: str  # ISO 3166-1 alpha-2 of the goods' origin, e.g. "CN"
    share_of_cogs: Decimal  # 0..1


@dataclass(frozen=True)
class MacroSignal:
    """A dated public fact about the world (v1: a tariff action), global to every borrower.

    Loaded only by the trusted loader. observed_at is when the fact became public (publication
    date), never its effective date (occurred_at). `payload_json` is canonical JSON of the parsed
    notice; it is external data: metrics read only codes, rates and dates from it.
    """

    source: str
    source_ref: str
    kind: str  # tariff_action
    payload_json: str
    observed_at: datetime
    occurred_at: datetime | None = None
    url: str | None = None
    sha256: str | None = None
    evidence: EvidenceRef | None = None


SignalSource = Literal["google_places", "yelp", "rdap"]


@dataclass(frozen=True)
class PublicSignal:
    """A public-web signal recorded by `record_signal` from a matched source result.

    Append-only like every ledger row: a re-pull is a new row with the same (source, source_ref),
    and `known` keeps the latest one observed by the as-of date. `payload_json` is canonical JSON
    of the parsed source result (kept as a string so the row stays hashable); it is untrusted
    data: metrics read only numbers, dates and flags from it, and text only through a versioned
    keyword rule. `match_ok` is resolved when the ledger is loaded: the entity match scored at or
    above the threshold with no analyst rejection, or an analyst confirmed it. Metrics use only
    rows with `match_ok`.
    """

    source: str
    source_ref: str
    kind: str  # listing | domain_registration
    payload_json: str
    observed_at: datetime
    occurred_at: datetime | None = None
    url: str | None = None
    sha256: str | None = None
    match_score: Decimal | None = None
    match_ok: bool = False
    evidence: EvidenceRef | None = None

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        return value if isinstance(value, dict) else {}


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


def _known_signals(rows: Iterable[PublicSignal], as_of: datetime) -> tuple[PublicSignal, ...]:
    """Signals are revised per (source, source_ref): a place id and a domain may collide."""
    known = known_as_of(rows, as_of, key=lambda r: f"{r.source}\x00{r.source_ref}")
    return tuple(sorted(known, key=lambda r: (r.observed_at, r.source, r.source_ref)))


def _known_macro(rows: Iterable[MacroSignal], as_of: datetime) -> tuple[MacroSignal, ...]:
    known = known_as_of(rows, as_of, key=lambda r: f"{r.source}\x00{r.source_ref}")
    return tuple(sorted(known, key=lambda r: (r.observed_at, r.source, r.source_ref)))


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
    public_signals: tuple[PublicSignal, ...] = ()
    macro_signals: tuple[MacroSignal, ...] = ()  # global rows, loaded with every ledger

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
            public_signals=_known_signals(self.public_signals, as_of),
            macro_signals=_known_macro(self.macro_signals, as_of),
        )
