"""Synthetic financial footprint around the real UCI Online Retail II orders.

The orders (see tangible.datasets.uci_retail) are real. Everything else is generated so the
metric and check library has a complete small-merchant ledger to run against: processor payouts
and their bank deposits, wholesale invoices paid by transfer, supplier invoices and outflows,
payroll, rent, VAT, ad spend, bank fees, owner loans and drawings, weekly inventory snapshots,
and a daily balance.

Deterministic: the same lines file, seed and plant set give byte-identical CSVs. Each component
draws from its own named random stream, so a plant does not shift the clean output of anything
else.

A clean run reconciles: each payout has one deposit of the same amount within 3 days, each paid
payable has a matching bank outflow, and each balance is the opening balance plus the cumulative
bank rows.

Time rule: a payable or receivable is written as two rows when it is paid. The issue row (paid_on
empty, observed_at = issued_on) and a revision row (same source_ref, paid_on set, observed_at the
day after payment). A lender at issue time never sees a payment date it could not have known.

CLI: python -m tangible.datasets.synthetic --out data/synthetic [--plant NAME ...]
"""

import argparse
import calendar
import csv
import hashlib
import json
import math
import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

from tangible.datasets.uci_retail import build_ledger, read_lines
from tangible.ledger.types import Ledger

PLANTS: Final[frozenset[str]] = frozenset(
    {
        "hidden_reserve",
        "undisclosed_lender_debit",
        "self_transfer_revenue",
        "partial_coverage",
        "phantom_payouts",
    }
)
PLANT_DESCRIPTIONS: Final[dict[str, str]] = {
    "hidden_reserve": (
        "Processor holds back 15% of each payout. Deposits are 15% below payout.amount and land "
        "4-6 days after normal settlement."
    ),
    "undisclosed_lender_debit": (
        "Fixed GBP 450.00 debit to YRDEAL CAPITAL on every business day, with no matching invoice."
    ),
    "self_transfer_revenue": (
        "Weekly round-amount transfers (GBP 5,000-9,000) from the owner's personal account, "
        "labelled SALES TRANSFER, booked as sales."
    ),
    "partial_coverage": (
        "Weekly AMAZON PAYMENTS deposits worth about 25% of net revenue, matching no order or "
        "payout."
    ),
    "phantom_payouts": (
        "Extra processor payouts and deposits worth about 12% of card sales, with no orders in "
        "the orders data."
    ),
}
PLANT_WINDOW_DAYS: Final[dict[str, int]] = {
    "hidden_reserve": 60,
    "undisclosed_lender_debit": 90,
    "self_transfer_revenue": 120,
    "partial_coverage": 120,
    "phantom_payouts": 90,
}

ACCOUNT_REF: Final = "GB-OPS-001"
PROCESSOR: Final = "SHOPIFY PAYMENTS"
OWNER_ACCOUNT: Final = "J SMITH PERSONAL"
LENDER: Final = "YRDEAL CAPITAL"
MARKETPLACE: Final = "AMAZON PAYMENTS"
BANK_NAME: Final = "NORTHVALE BANK PLC"
LANDLORD: Final = "HOLLOWAY PROPERTY LTD"
ONE_DAY: Final = timedelta(days=1)
CENT: Final = Decimal("0.01")
MICRO: Final = Decimal("0.0001")
ZERO: Final = Decimal(0)
NAMESPACE: Final = uuid.NAMESPACE_URL

CARD_PCT: Final = Decimal("0.022")
CARD_PER_ORDER: Final = Decimal("0.20")
WHOLESALE_MIN: Final = Decimal(500)
RESERVE_KEEP: Final = Decimal("0.85")
PAYOUT_SETTLE_BUFFER_DAYS: Final = 10  # covers posting (up to 3 days) plus a planted 6-day delay
DEPOSIT_LATE_P: Final = 0.008
RECEIVABLE_ON_TIME_P: Final = 0.92
SUPPLIER_LATE_P: Final = 0.12
TERMS_DAYS: Final = 30
DEMAND_DAYS: Final = 56
COVER_DAYS: Final = 35
TOP_SKUS: Final = 300
AGED_SKUS: Final = 6
AGED_DAYS: Final = 90
DRAWINGS_MIN: Final = Decimal(1000)
DRAWINGS_SHARE: Final = Decimal("0.6")  # of the cash above the working-capital buffer, monthly
CASH_BUFFER: Final = Decimal(120000)
CASH_FLOOR: Final = Decimal(20000)  # below this the owner lends the company cash
LOAN_CUSHION: Final = Decimal(15000)
OPENING_STOCK_DAYS: Final = 182  # SKUs first sold within this many days are on the shelf at day 0
RENT: Final = Decimal("4250.00")
ACCOUNT_FEE: Final = Decimal("25.00")
CREDIT_NOTE_DAYS: Final = (
    3,
    12,
)  # wholesale credit notes are refunded by transfer this many days on
LENDER_DEBIT: Final = Decimal("450.00")
NON_PRODUCT_CODES: Final = frozenset({"POST", "DOT", "M", "PADS", "C2", "CRUK", "S"})
SUPPLIERS: Final[tuple[tuple[str, str], ...]] = (  # (name, invoice code)
    ("HARBOUR GIFTWARE LTD", "HGL"),
    ("NORTHGATE PAPERCRAFT LTD", "NPC"),
    ("LANTERN IMPORTS LTD", "LNI"),
    ("EASTWIND TRADING CO LTD", "ETW"),
    ("BRIGHTWELL WHOLESALE LTD", "BWL"),
    ("MERIDIAN PACKAGING LTD", "MPK"),
    ("KESTREL DISTRIBUTION LTD", "KDL"),
)
UTILITIES: Final = ("WESSEX POWER AND GAS", "UTL")
SOFTWARE: Final = ("SAASWORKS LTD", "SAS", Decimal("240.00"))
COURIER: Final = ("PARCELPOINT LOGISTICS LTD", "PPL")
COST_MEAN: Final = 0.45  # supplier cost as a share of a SKU's median selling price (gift trade)
COST_SD: Final = 0.07
ONE_OFFS: Final[tuple[tuple[str, str, Decimal], ...]] = (
    ("SHELFCRAFT FIXTURES LTD", "SHF", Decimal("3450.00")),
    ("ALBION INSURANCE BROKERS LTD", "ALB", Decimal("2180.00")),
    ("MARLOW AND PEARS LLP", "MRL", Decimal("1240.00")),
)
HEADERS: Final[dict[str, tuple[str, ...]]] = {
    "payout": ("source_ref", "processor", "amount", "fees", "expected_on", "settled_on"),
    "bank_txn": (
        "source_ref",
        "account_ref",
        "amount",
        "direction",
        "counterparty",
        "description",
        "posted_on",
    ),
    "invoice": (
        "source_ref",
        "kind",
        "counterparty",
        "amount",
        "issued_on",
        "due_on",
        "paid_on",
    ),
    "inventory_snapshot": ("source_ref", "sku", "units_on_hand", "unit_cost", "as_of"),
    "account_balance": ("source_ref", "account_ref", "balance", "as_of"),
}
EVIDENCE_COLUMNS: Final = ("observed_at", "evidence_pull_id", "evidence_locator")
EVIDENCE_PULL_COLUMNS: Final = (
    "id",
    "source",
    "connection_id",
    "fetched_at",
    "s3_key",
    "sha256",
    "byte_size",
    "status",
)


# ----------------------------------------------------------------------------- profile


@dataclass(frozen=True)
class Profile:
    """Who a synthetic merchant is: the names its bank rows and invoices carry.

    The default reproduces the original single-merchant output byte for byte. Checks key on some
    of these names, so a replacement must keep what they look for (see tangible.checks):

    - `processor_name` is the bank counterparty of payout deposits; partial_coverage reads its
      processor keyword (SHOPIFY, STRIPE, SQUARE, ...) against the connected sources.
    - `processor_source` is the payout's processor and the evidence pull source, lower case; its
      upper-case form prefixes each deposit's description ("SHOPIFY PAYOUT <ref>").
    - `owner_account` is the owner's personal account. self_transfers_as_revenue finds it because
      it receives outflows described OWNER DRAWINGS (that description is fixed).
    - `lender_name` should keep a lender word (CAPITAL, LENDING, FINANCE, ...) so the recurring
      debit is flagged as lender-like.
    - `marketplace_name` must keep the AMAZON keyword for partial_coverage.
    - `supplier_names` replaces the seven wholesale suppliers' names (empty keeps the defaults).
    """

    account_ref: str = ACCOUNT_REF
    processor_name: str = PROCESSOR
    processor_source: str = "shopify"
    owner_account: str = OWNER_ACCOUNT
    lender_name: str = LENDER
    marketplace_name: str = MARKETPLACE
    bank_name: str = BANK_NAME
    landlord: str = LANDLORD
    supplier_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.supplier_names and len(self.supplier_names) != len(SUPPLIERS):
            raise ValueError(f"supplier_names needs {len(SUPPLIERS)} names or none")
        if len(set(self.supplier_names)) != len(self.supplier_names):
            raise ValueError("supplier_names must be distinct")

    def supplier(self, index: int) -> tuple[str, str]:
        """(name, invoice code) of supplier `index`."""
        name, code = SUPPLIERS[index]
        return (self.supplier_names[index] if self.supplier_names else name, code)


DEFAULT_PROFILE: Final = Profile()


def _describe(plant: str, profile: Profile) -> str:
    """The plant's description with the profile's names in place of the default merchant's."""
    return (
        PLANT_DESCRIPTIONS[plant]
        .replace(LENDER, profile.lender_name)
        .replace(OWNER_ACCOUNT, profile.owner_account)
        .replace(MARKETPLACE, profile.marketplace_name)
    )


# ----------------------------------------------------------------------------- helpers


def _money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _cents(x: Decimal) -> str:
    return f"{_money(x):.2f}"


def _micro(x: Decimal) -> str:
    return f"{x.quantize(MICRO, rounding=ROUND_HALF_UP):.4f}"


def _rng(seed: int, name: str) -> random.Random:
    return random.Random(f"{seed}:{name}")


def _at(d: date, hour: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def _roll(d: date) -> date:
    """The same day, or the next business day when it falls on a weekend."""
    while d.weekday() >= 5:
        d += ONE_DAY
    return d


def _add_bdays(d: date, n: int) -> date:
    for _ in range(n):
        d = _roll(d + ONE_DAY)
    return d


def _days(a: date, b: date) -> list[date]:
    """Every day from a to b, both included."""
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _span(a: date, b: date) -> list[date]:
    """Every day from a up to but not including b."""
    return [a + timedelta(days=i) for i in range((b - a).days)]


def _bdays(a: date, b: date) -> list[date]:
    return [d for d in _days(a, b) if d.weekday() < 5]


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _last_bday(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= ONE_DAY
    return d


def _months(a: date, b: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _signed(t: "_Txn") -> Decimal:
    return t.amount if t.direction == "in" else -t.amount


# ----------------------------------------------------------------------------- records


@dataclass(frozen=True)
class _Cal:
    start: date
    horizon: date

    @property
    def n_days(self) -> int:
        return (self.horizon - self.start).days + 1


@dataclass(frozen=True)
class _Txn:
    """A bank row before it is numbered. direction is 'in' or 'out'; amount is positive."""

    posted: date
    direction: str
    amount: Decimal
    counterparty: str
    description: str


@dataclass
class _Payout:
    ref: str
    expected_on: date
    settled_on: date
    amount: Decimal  # what the processor reports: card sales less refunds less fees
    fees: Decimal
    card_sales: Decimal
    card_orders: int
    deposit_on: date
    deposit_amount: Decimal  # what reaches the bank; differs from amount only when planted
    phantom: bool = False


@dataclass(frozen=True)
class _Wholesale:
    invoice: str
    customer: str | None
    issued: date
    net: Decimal


@dataclass(frozen=True)
class _Credit:
    """A credit note against a wholesale account, refunded by bank transfer."""

    ref: str
    customer: str | None
    issued: date
    amount: Decimal


@dataclass(frozen=True)
class _Entry:
    table: str
    source: str  # evidence source: shopify, bank, accounting, inventory
    observed_at: datetime
    body: tuple[str, ...]  # columns before observed_at, in HEADERS order


@dataclass
class _Out:
    entries: list[_Entry] = field(default_factory=list)
    txns: list[_Txn] = field(default_factory=list)

    def add(self, table: str, source: str, observed_at: datetime, *body: str) -> None:
        self.entries.append(_Entry(table, source, observed_at, body))


@dataclass
class _Sales:
    card_net: defaultdict[date, Decimal]
    card_orders: defaultdict[date, int]
    refunds: defaultdict[date, Decimal]  # card refunds, taken off the next processor payout
    whole_net: defaultdict[date, Decimal]
    wholesale: list[_Wholesale]
    credits: list[_Credit]
    whole_credits: defaultdict[date, Decimal]

    def rev(self, d: date) -> Decimal:
        """Net revenue for the day: sales less card refunds and wholesale credit notes."""
        return self.card_net[d] + self.whole_net[d] - self.refunds[d] - self.whole_credits[d]


@dataclass
class _SkuSales:
    skus: list[str]
    units: list[list[int]]  # per SKU, per day index
    prices: list[list[Decimal]]
    revenue: list[Decimal]
    credit_customer: dict[str, str | None]  # credit note invoice -> customer on the note


@dataclass
class _Stock:
    receipts: list[tuple[int, int, int]]  # (day index, sku index, units received)
    eod: dict[int, list[int]]  # Monday day index -> units on hand at end of day
    on_hand: list[int]
    last_sale: list[int]
    live_from: list[int]  # first day the SKU is stocked; no snapshots before it


# ----------------------------------------------------------------------------- loading


def _load_sales(ledger: Ledger, credit_customer: dict[str, str | None]) -> _Sales:
    """Split orders by channel, and route each credit note the way its order was paid.

    The data has no link from a credit note to its order. The note's customer is matched to
    that customer's latest earlier order: a wholesale order means a wholesale credit note
    (refunded by transfer), a card order means a card refund (comes off the next processor
    payout). Notes with no customer or no earlier order fall back to the order-size threshold.
    """
    sales = _Sales(
        card_net=defaultdict(Decimal),
        card_orders=defaultdict(int),
        refunds=defaultdict(Decimal),
        whole_net=defaultdict(Decimal),
        wholesale=[],
        credits=[],
        whole_credits=defaultdict(Decimal),
    )
    events: list[tuple[datetime, int, str]] = [
        (o.occurred_at, 0, o.source_ref) for o in ledger.orders
    ] + [(r.occurred_at, 1, r.source_ref) for r in ledger.refunds]
    orders = {o.source_ref: o for o in ledger.orders}
    refunds = {r.source_ref: r for r in ledger.refunds}
    last_channel: dict[str, bool] = {}  # customer -> latest order was wholesale
    for _, kind, ref in sorted(events):
        if kind == 0:
            o = orders[ref]
            d = o.occurred_at.date()
            net = o.gross - o.discounts
            is_whole = net >= WHOLESALE_MIN
            if is_whole:
                sales.whole_net[d] += net
                sales.wholesale.append(_Wholesale(o.source_ref, o.customer_ref, d, net))
            else:
                sales.card_net[d] += net
                if net > 0:
                    sales.card_orders[d] += 1
            if o.customer_ref:
                last_channel[o.customer_ref] = is_whole
            continue
        r = refunds[ref]
        d = r.occurred_at.date()
        customer = credit_customer.get(ref)
        whole = last_channel.get(customer) if customer else None
        if whole is None:
            whole = r.amount >= WHOLESALE_MIN
        if whole:
            sales.credits.append(_Credit(ref, customer, d, r.amount))
            sales.whole_credits[d] += r.amount
        else:
            sales.refunds[d] += r.amount
    return sales


def _read_sku_sales(lines_csv: Path, cal: _Cal) -> _SkuSales:
    """Units and prices per product SKU. Credit notes, discounts and non-product codes are out."""
    index: dict[str, int] = {}
    units: list[list[int]] = []
    prices: list[list[Decimal]] = []
    revenue: list[Decimal] = []
    credit_customer: dict[str, str | None] = {}
    for ln in read_lines(lines_csv):
        code = ln.stock_code
        if ln.invoice.startswith("C"):
            credit_customer.setdefault(ln.invoice, ln.customer_id)
            continue
        if ln.quantity <= 0 or code == "D":
            continue
        if code in NON_PRODUCT_CODES or code.startswith("GIFT_"):
            continue
        day = (ln.occurred_at.date() - cal.start).days
        if not 0 <= day < cal.n_days:
            continue
        k = index.get(code)
        if k is None:
            k = len(units)
            index[code] = k
            units.append([0] * cal.n_days)
            prices.append([])
            revenue.append(ZERO)
        units[k][day] += ln.quantity
        prices[k].append(ln.price)
        revenue[k] += ln.price * ln.quantity
    order = sorted(index)
    return _SkuSales(
        skus=order,
        units=[units[index[c]] for c in order],
        prices=[prices[index[c]] for c in order],
        revenue=[revenue[index[c]] for c in order],
        credit_customer=credit_customer,
    )


# ----------------------------------------------------------------------------- processor


def _deposit_day(settled: date, rng: random.Random) -> date:
    """Bank posting day for a processor settlement: same day or the next business day, and
    rarely one business day later still (only where that stays within 3 calendar days)."""
    normal = settled if rng.random() < 0.5 else _roll(settled + ONE_DAY)
    late = _add_bdays(settled, 2)
    if rng.random() < DEPOSIT_LATE_P and (late - settled).days <= 3:
        return late
    return normal


def _build_payouts(cal: _Cal, sales: _Sales, rng: random.Random) -> list[_Payout]:
    """One payout per business day covering the calendar days since the previous one.

    Credit notes reduce the next payout. A window that nets to zero or below carries forward.
    """
    payouts: list[_Payout] = []
    prev = cal.start
    carry = ZERO
    for expected in _bdays(cal.start, cal.horizon + timedelta(days=4)):
        window = _span(prev, expected)
        prev = expected
        card = sum((sales.card_net[d] for d in window), ZERO)
        orders = sum(sales.card_orders[d] for d in window)
        refunds = sum((sales.refunds[d] for d in window), ZERO)
        fees = _money(card * CARD_PCT + CARD_PER_ORDER * orders)
        net = _money(card - refunds - fees) + carry
        if net <= 0:
            carry = net
            continue
        carry = ZERO
        settled = _add_bdays(expected, 2)
        deposit = _deposit_day(settled, rng)
        payouts.append(
            _Payout(
                ref=f"PO-{expected:%Y%m%d}",
                expected_on=expected,
                settled_on=settled,
                amount=net,
                fees=fees,
                card_sales=card,
                card_orders=orders,
                deposit_on=deposit,
                deposit_amount=net,
            )
        )
    return payouts


def _phantom_payouts(real: list[_Payout], from_day: date, rng: random.Random) -> list[_Payout]:
    """Extra payouts on the same days as real ones, sized at about 12% of card sales."""
    phantoms: list[_Payout] = []
    for p in real:
        if p.expected_on < from_day or p.card_sales <= 0:
            continue
        rate = Decimal(str(round(rng.uniform(0.11, 0.13), 4)))
        gross = _money(p.card_sales * rate)
        count = int((Decimal(p.card_orders) * rate).quantize(Decimal(1), ROUND_HALF_UP))
        fees = _money(gross * CARD_PCT + CARD_PER_ORDER * count)
        amount = _money(gross - fees)
        settled = _add_bdays(p.expected_on, 2)
        phantoms.append(
            _Payout(
                ref=f"PO-PH-{p.expected_on:%Y%m%d}",
                expected_on=p.expected_on,
                settled_on=settled,
                amount=amount,
                fees=fees,
                card_sales=gross,
                card_orders=count,
                deposit_on=_deposit_day(settled, rng),
                deposit_amount=amount,
                phantom=True,
            )
        )
    return phantoms


# ----------------------------------------------------------------------------- records


def _record_invoice(
    out: _Out,
    kind: str,
    ref: str,
    counterparty: str,
    amount: Decimal,
    issued: date,
    due: date,
    paid: date | None,
    horizon: date,
) -> None:
    """Issue row now; a revision row once paid (only if the payment is inside the data)."""
    base = (ref, kind, counterparty, _cents(amount), issued.isoformat(), due.isoformat())
    out.add("invoice", "accounting", _at(issued), *base, "")
    if paid is not None and paid <= horizon:
        out.add("invoice", "accounting", _at(paid + ONE_DAY, 6), *base, paid.isoformat())


def _add_wholesale(out: _Out, sales: _Sales, cal: _Cal, rng: random.Random) -> None:
    """Orders of GBP 500 or more: receivable invoices paid by transfer after 14-30 days."""
    for w in sales.wholesale:
        due = _roll(w.issued + timedelta(days=TERMS_DAYS))
        on_time = rng.random() < RECEIVABLE_ON_TIME_P
        early = rng.randint(14, 28)
        slip = rng.randint(5, 25)
        if on_time:
            paid = _roll(w.issued + timedelta(days=early))
        else:
            paid = _roll(due + timedelta(days=slip))
        # Anonymous orders still need a payer name on the bank line; one name per invoice.
        counterparty = f"CUSTOMER {w.customer}" if w.customer else f"GUEST PAYER {w.invoice}"
        _record_invoice(
            out,
            "receivable",
            f"INV-{w.invoice}",
            counterparty,
            _money(w.net),
            w.issued,
            due,
            paid,
            cal.horizon,
        )
        if paid <= cal.horizon:
            out.txns.append(
                _Txn(paid, "in", _money(w.net), counterparty, f"PAYMENT INV {w.invoice}")
            )
    for c in sales.credits:
        counterparty = f"CUSTOMER {c.customer}" if c.customer else f"GUEST PAYER {c.ref}"
        refunded = _roll(c.issued + timedelta(days=rng.randint(*CREDIT_NOTE_DAYS)))
        amount = _money(c.amount)
        # A credit note is a negative receivable, settled when the money goes back out.
        _record_invoice(
            out, "receivable", f"CN-{c.ref}", counterparty, -amount, c.issued, c.issued,
            refunded, cal.horizon,
        )  # fmt: skip
        if refunded <= cal.horizon:
            out.txns.append(_Txn(refunded, "out", amount, counterparty, f"REFUND CN {c.ref}"))


def _add_payable(
    out: _Out,
    cal: _Cal,
    rng: random.Random,
    counterparty: str,
    ref: str,
    amount: Decimal,
    issued: date,
    terms: int = TERMS_DAYS,
    late_p: float = SUPPLIER_LATE_P,
) -> None:
    """Supplier invoice, net 30 by default. Paid on the due date, or 3-20 days late."""
    due = _roll(issued + timedelta(days=terms))
    late = rng.random() < late_p
    slip = rng.randint(3, 20)
    paid = _roll(due + timedelta(days=slip)) if late else due
    _record_invoice(out, "payable", ref, counterparty, amount, issued, due, paid, cal.horizon)
    if paid <= cal.horizon:
        out.txns.append(_Txn(paid, "out", _money(amount), counterparty, f"PAYMENT {ref}"))


# ----------------------------------------------------------------------------- inventory


def _simulate(units: list[list[int]], leads: list[int], start: date, n_days: int) -> _Stock:
    """Daily stock for every product SKU.

    Stock falls by real units sold. A SKU is reordered when its position falls below lead time
    plus three weeks of demand plus a safety margin of half its biggest day so far, in a lot of
    about five weeks. A bulk order bigger than what is on the shelf is filled by a same-day rush
    lot from the supplier, so stock never goes negative. SKUs first sold in the first six months
    are on the shelf at day 0 (about five weeks of their first eight selling weeks); later new
    lines get an introductory lot a week before their first sale.
    """
    count = len(units)
    on_hand = [0] * count
    on_order = [0] * count
    live_from = [0] * count
    arrivals: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    receipts: list[tuple[int, int, int]] = []
    for k, s in enumerate(units):
        first = next((d for d, u in enumerate(s) if u), n_days)
        if first < OPENING_STOCK_DAYS:
            # In stock at day 0, sized on the eight weeks from its first sale (slow sellers sit).
            window = sum(s[first : first + DEMAND_DAYS])
            on_hand[k] = math.ceil(window * COVER_DAYS / DEMAND_DAYS)
        elif first < n_days:
            qty = max(6, math.ceil(sum(s[first : first + DEMAND_DAYS]) * COVER_DAYS / DEMAND_DAYS))
            arrive = first - 7
            on_order[k] += qty
            arrivals[arrive].append((k, qty))
            live_from[k] = arrive
    trailing = [0] * count
    peak = [0] * count
    last_sale = [-1] * count
    eod: dict[int, list[int]] = {}
    for d in range(n_days):
        for k, qty in arrivals.pop(d, []):
            on_hand[k] += qty
            on_order[k] -= qty
            receipts.append((d, k, qty))
        for k in range(count):
            s = units[k]
            sold = s[d]
            if sold:
                if sold > on_hand[k]:
                    rush = sold - on_hand[k] + max(6, math.ceil(trailing[k] / DEMAND_DAYS * 14))
                    on_hand[k] += rush
                    receipts.append((d, k, rush))
                on_hand[k] -= sold
                last_sale[k] = d
                peak[k] = max(peak[k], sold)
            trailing[k] += sold - (s[d - DEMAND_DAYS] if d >= DEMAND_DAYS else 0)
        for k in range(count):
            if trailing[k] <= 0:
                continue
            avg = trailing[k] / DEMAND_DAYS
            lead = leads[k]
            if on_hand[k] + on_order[k] <= avg * (lead + 21) + peak[k] / 2:
                qty = max(6, math.ceil(avg * COVER_DAYS))
                on_order[k] += qty
                if d + lead < n_days:
                    arrivals[d + lead].append((k, qty))
        if (start + timedelta(days=d)).weekday() == 0:
            eod[d] = on_hand.copy()
    return _Stock(
        receipts=receipts, eod=eod, on_hand=on_hand, last_sale=last_sale, live_from=live_from
    )


def _add_supplier_invoices(
    out: _Out,
    cal: _Cal,
    stock: _Stock,
    cost_on: Callable[[int, date], Decimal],
    supplier_of: list[int],
    rng: random.Random,
    profile: Profile,
) -> list[tuple[date, Decimal]]:
    """One invoice per supplier per week, for the stock received that week, issued the Monday
    after. Returns (issued, amount) for the VAT input calculation."""
    weekly: defaultdict[tuple[int, date], Decimal] = defaultdict(Decimal)
    for d, k, qty in stock.receipts:
        day = cal.start + timedelta(days=d)
        weekly[(supplier_of[k], _monday(day))] += cost_on(k, day) * qty
    purchases: list[tuple[date, Decimal]] = []
    for sup, week in sorted(weekly):
        issued = week + timedelta(days=7)
        amount = _money(weekly[(sup, week)])
        if issued > cal.horizon or amount <= 0:
            continue
        name, code = profile.supplier(sup)
        _add_payable(out, cal, rng, name, f"PI-{code}-{issued:%Y%m%d}", amount, issued)
        purchases.append((issued, amount))
    return purchases


def _add_snapshots(
    out: _Out,
    cal: _Cal,
    sku_names: list[str],
    stock: _Stock,
    cost_on: Callable[[int, date], Decimal],
    chosen: list[int],
) -> None:
    for d in sorted(stock.eod):
        as_of = cal.start + timedelta(days=d)
        for k in chosen:
            if d < stock.live_from[k]:
                continue
            name = sku_names[k]
            out.add(
                "inventory_snapshot",
                "inventory",
                _at(as_of + ONE_DAY, 6),
                f"STK-{name}-{as_of:%Y%m%d}",
                name,
                str(stock.eod[d][k]),
                _micro(cost_on(k, as_of)),
                as_of.isoformat(),
            )


# ----------------------------------------------------------------------------- outflows


def _add_monthly(
    out: _Out,
    cal: _Cal,
    sales: _Sales,
    purchases: list[tuple[date, Decimal]],
    payroll_rng: random.Random,
    ads_rng: random.Random,
    opex_rng: random.Random,
    profile: Profile,
) -> None:
    """Monthly: rent, utilities, software, courier invoices, bank fees, payroll. Weekly: ad
    platform invoices. Quarterly: VAT.

    Rent, utilities, software, courier and ad platform charges arrive as payable invoices (the
    accounting system holds the bill) and are paid on the due date. Payroll runs through
    journals and VAT through the return, so those two, bank fees and owner drawings are bank
    outflows with no invoice.
    """
    vat_inputs = list(purchases)
    for y, m in _months(cal.start, cal.horizon):
        first = date(y, m, 1)
        label = first.strftime("%b %Y")
        ym = f"{y}{m:02d}"
        month_last = date(y, m, calendar.monthrange(y, m)[1])
        month_end = _last_bday(y, m)
        if cal.start <= first <= cal.horizon:
            _add_payable(out, cal, opex_rng, profile.landlord, f"RENT-{ym}", RENT, first, 0, 0.0)
            winter = 1 if m in (11, 12, 1, 2, 3) else 0
            power = _money(Decimal(str(round(310 + 95 * winter, 2))) * Decimal(
                str(round(opex_rng.uniform(0.88, 1.12), 4))
            ))  # fmt: skip
            name, code = UTILITIES
            _add_payable(out, cal, opex_rng, name, f"{code}-{ym}", power, first, 14, 0.0)
            vat_inputs.append((first, power))
            sw_name, sw_code, sw_amount = SOFTWARE
            _add_payable(out, cal, opex_rng, sw_name, f"{sw_code}-{ym}", sw_amount, first, 0, 0.0)
            vat_inputs.append((first, sw_amount))
        if cal.start <= month_end <= cal.horizon:
            card_orders = sum(sales.card_orders[d] for d in _days(first, month_last))
            whole_orders = sum(1 for w in sales.wholesale if first <= w.issued <= month_last)
            courier = _money(
                (Decimal("3.40") * card_orders + Decimal("11.00") * whole_orders)
                * Decimal(str(round(opex_rng.uniform(0.95, 1.05), 4)))
            )
            if courier > 0:
                name, code = COURIER
                _add_payable(out, cal, opex_rng, name, f"{code}-{ym}", courier, month_end, 14, 0.05)
                vat_inputs.append((month_end, courier))

        if cal.start <= month_end <= cal.horizon:
            out.txns.append(_Txn(month_end, "out", ACCOUNT_FEE, profile.bank_name, "ACCOUNT FEES"))
        rate = Decimal(str(round(payroll_rng.uniform(0.11, 0.13), 4)))
        revenue = sum((sales.rev(d) for d in _days(first, month_last)), ZERO)
        if month_end <= cal.horizon and revenue > 0:
            hundreds = (revenue * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            out.txns.append(
                _Txn(month_end, "out", hundreds * 100, "STAFF PAYROLL", f"SALARIES {label}")
            )

    season = {9: Decimal("1.35"), 10: Decimal("1.35"), 11: Decimal("1.35"), 12: Decimal("1.2")}
    monday = _monday(cal.start)
    while monday <= cal.horizon:
        rate = Decimal(str(round(ads_rng.uniform(0.05, 0.07), 4)))
        prior = sum((sales.rev(d) for d in _span(monday - timedelta(days=7), monday)), ZERO)
        post = _roll(monday + ONE_DAY)
        total = _money(prior * rate * season.get(post.month, Decimal(1)))
        if total > 0 and post <= cal.horizon:
            google = _money(total * Decimal("0.55"))
            week = f"{monday:%Y%m%d}"
            # Platform invoices are issued on the charge date and settled by card/direct debit.
            _add_payable(out, cal, opex_rng, "GOOGLE ADS", f"ADS-G-{week}", google, post, 0, 0.0)
            _add_payable(
                out, cal, opex_rng, "META ADS", f"ADS-M-{week}", total - google, post, 0, 0.0
            )
        monday += timedelta(days=7)

    for y, m in _months(cal.start, cal.horizon):
        if m % 3 != 0:
            continue
        quarter_end = date(y, m, calendar.monthrange(y, m)[1])
        quarter_start = date(y, m - 2, 1)
        due_month = m + 2
        due_year = y + (due_month - 1) // 12
        due = _roll(date(due_year, (due_month - 1) % 12 + 1, 1) + timedelta(days=6))
        if due > cal.horizon:
            continue
        output = sum((sales.rev(d) for d in _days(quarter_start, quarter_end)), ZERO)
        inputs = sum(
            (amt for issued, amt in vat_inputs if quarter_start <= issued <= quarter_end), ZERO
        )
        net = _money((output - inputs) / 6)
        if net > 0:
            out.txns.append(_Txn(due, "out", net, "HMRC VAT", f"VAT {y} Q{m // 3}"))


# ----------------------------------------------------------------------------- plants


def _plant_lender_debit(out: _Out, cal: _Cal, from_day: date, profile: Profile) -> None:
    for d in _bdays(from_day, cal.horizon):
        out.txns.append(
            _Txn(d, "out", LENDER_DEBIT, profile.lender_name, f"DD {profile.lender_name}")
        )


def _plant_self_transfer(
    out: _Out, cal: _Cal, from_day: date, rng: random.Random, profile: Profile
) -> None:
    for d in _days(from_day, cal.horizon):
        if d.weekday() == 4:
            amount = Decimal(rng.randint(10, 18) * 500)
            out.txns.append(_Txn(d, "in", amount, profile.owner_account, "SALES TRANSFER"))


def _plant_partial_coverage(
    out: _Out, cal: _Cal, sales: _Sales, from_day: date, rng: random.Random, profile: Profile
) -> None:
    for d in _days(from_day, cal.horizon):
        if d.weekday() != 3:
            continue
        week = sum(
            (sales.rev(x) for x in _days(d - timedelta(days=3), d + timedelta(days=3))), ZERO
        )
        factor = Decimal(str(round(0.25 * rng.uniform(0.97, 1.03), 4)))
        amount = _money(week * factor)
        if amount > 0:
            out.txns.append(
                _Txn(d, "in", amount, profile.marketplace_name, "AMAZON MARKETPLACE DISBURSEMENT")
            )


# ----------------------------------------------------------------------------- balance


def _settle_owner(out: _Out, cal: _Cal, opening: Decimal, profile: Profile) -> None:
    """Owner cash flows, simulated day by day from the opening balance.

    When cash would fall under the floor (a restock squeeze) the owner lends the company enough
    to clear it, in round GBP 5,000 amounts. At each month end the owner draws 60% of the cash
    above the working-capital buffer. Plants are already in `out.txns`.
    """
    net: defaultdict[date, Decimal] = defaultdict(Decimal)
    for t in out.txns:
        net[t.posted] += _signed(t)
    month_ends = {_last_bday(y, m) for y, m in _months(cal.start, cal.horizon)}
    balance = opening
    for d in _days(cal.start, cal.horizon):
        balance += net[d]
        if balance < CASH_FLOOR:
            need = CASH_FLOOR + LOAN_CUSHION - balance
            loan = Decimal(math.ceil(need / 5000) * 5000)
            out.txns.append(_Txn(d, "in", loan, profile.owner_account, "DIRECTORS LOAN"))
            balance += loan
        excess = balance - CASH_BUFFER
        if d in month_ends and excess >= DRAWINGS_MIN:
            draw = Decimal(int(excess * DRAWINGS_SHARE // 100) * 100)
            if draw >= DRAWINGS_MIN:
                out.txns.append(_Txn(d, "out", draw, profile.owner_account, "OWNER DRAWINGS"))
                balance -= draw


def _add_bank_rows(out: _Out, profile: Profile) -> None:
    """Number bank rows in a fixed order. Each is a deposit or outflow; observed the morning
    after it posts."""
    txns = sorted(
        out.txns,
        key=lambda t: (t.posted, t.counterparty, t.description, t.direction, _cents(t.amount)),
    )
    for i, t in enumerate(txns, 1):
        out.add(
            "bank_txn",
            "bank",
            _at(t.posted + ONE_DAY, 6),
            f"BT-{i:06d}",
            profile.account_ref,
            _cents(t.amount),
            t.direction,
            t.counterparty,
            t.description,
            t.posted.isoformat(),
        )


def _add_balances(out: _Out, cal: _Cal, opening: Decimal, profile: Profile) -> None:
    """Daily end-of-day balance: the opening balance plus the running sum of bank rows."""
    net: defaultdict[date, Decimal] = defaultdict(Decimal)
    for t in out.txns:
        net[t.posted] += _signed(t)
    running = ZERO
    cumulative: list[tuple[date, Decimal]] = []
    for d in _days(cal.start, cal.horizon):
        running += net[d]
        cumulative.append((d, running))
    for d, c in cumulative:
        out.add(
            "account_balance",
            "bank",
            _at(d + ONE_DAY, 6),
            f"BAL-{d:%Y%m%d}",
            profile.account_ref,
            _cents(opening + c),
            d.isoformat(),
        )


# ----------------------------------------------------------------------------- writing


def _write(out_dir: Path, entries: list[_Entry]) -> dict[str, int]:
    """Assign evidence pulls (one per source and observed month) and row locators, then write
    one CSV per table plus evidence_pull.csv."""
    by_pull: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(entries):
        by_pull[(e.source, e.observed_at.strftime("%Y-%m"))].append(i)

    pull_ids: dict[tuple[str, str], str] = {}
    locator: dict[int, str] = {}
    pull_rows: list[list[str]] = []
    for source, month in sorted(by_pull):
        pull_id = str(uuid.uuid5(NAMESPACE, f"tangible-synthetic|{source}|{month}"))
        pull_ids[(source, month)] = pull_id
        idxs = sorted(
            by_pull[(source, month)],
            key=lambda i: (entries[i].table, entries[i].observed_at, entries[i].body),
        )
        lines: list[str] = []
        for n, i in enumerate(idxs, 1):
            locator[i] = f"row={n}"
            e = entries[i]
            lines.append("|".join((e.table, *e.body, e.observed_at.isoformat())))
        payload = ("\n".join(lines) + "\n").encode()
        year, mon = int(month[:4]), int(month[5:])
        next_year, next_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
        fetched = datetime(next_year, next_mon, 1, 3, tzinfo=UTC)
        pull_rows.append(
            [
                pull_id,
                source,
                "",
                fetched.isoformat(),
                f"synthetic/{source}/{month}.csv",
                hashlib.sha256(payload).hexdigest(),
                str(len(payload)),
                "complete",
            ]
        )

    counts: dict[str, int] = {}
    for table, body_cols in HEADERS.items():
        idxs = sorted(
            (i for i, e in enumerate(entries) if e.table == table),
            key=lambda i: (entries[i].observed_at, entries[i].body),
        )
        rows: list[list[str]] = []
        for i in idxs:
            e = entries[i]
            pull_id = pull_ids[(e.source, e.observed_at.strftime("%Y-%m"))]
            rows.append([*e.body, e.observed_at.isoformat(), pull_id, locator[i]])
        _write_csv(out_dir / f"{table}.csv", (*body_cols, *EVIDENCE_COLUMNS), rows)
        counts[table] = len(rows)
    _write_csv(out_dir / "evidence_pull.csv", EVIDENCE_PULL_COLUMNS, pull_rows)
    counts["evidence_pull"] = len(pull_rows)
    return counts


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


# ----------------------------------------------------------------------------- generate


def generate(
    lines_csv: Path,
    out_dir: Path,
    seed: int = 7,
    plant: frozenset[str] = frozenset(),
    profile: Profile = DEFAULT_PROFILE,
) -> dict[str, object]:
    """Write the synthetic footprint for the orders in `lines_csv` into `out_dir`.

    `profile` names the merchant (bank account, processor, owner, lender, ...); the default is the
    original single-merchant footprint. Names never change the random streams.

    Returns a summary: the row counts per file, the opening balance, and the planted list.
    """
    unknown = sorted(plant - PLANTS)
    if unknown:
        raise ValueError(f"unknown plant(s) {unknown}; choose from {sorted(PLANTS)}")

    root = random.Random(seed)
    ledger = build_ledger(lines_csv)
    dates = [o.occurred_at.date() for o in ledger.orders]
    dates += [r.occurred_at.date() for r in ledger.refunds]
    if not dates:
        raise ValueError(f"no orders found in {lines_csv}")
    cal = _Cal(start=min(dates), horizon=max(dates))
    sku = _read_sku_sales(lines_csv, cal)
    sales = _load_sales(ledger, sku.credit_customer)

    def window(name: str) -> date:
        return max(cal.start, cal.horizon - timedelta(days=PLANT_WINDOW_DAYS[name] - 1))

    out = _Out()

    # Processor payouts and their deposits.
    payouts = _build_payouts(cal, sales, _rng(seed, "payout_timing"))
    if "hidden_reserve" in plant:
        reserve_rng = _rng(seed, "plant:hidden_reserve")
        for p in payouts:
            if p.expected_on >= window("hidden_reserve"):
                p.deposit_amount = _money(p.amount * RESERVE_KEEP)
                p.deposit_on = _roll(p.deposit_on + timedelta(days=reserve_rng.randint(4, 6)))
    if "phantom_payouts" in plant:
        payouts += _phantom_payouts(
            payouts, window("phantom_payouts"), _rng(seed, "plant:phantom_payouts")
        )
    # Payouts still settling at the end of the data are left out in every run, so a planted
    # deposit delay never strands a payout without its deposit at the cutoff.
    payout_cutoff = cal.horizon - timedelta(days=PAYOUT_SETTLE_BUFFER_DAYS)
    payouts = [p for p in payouts if p.settled_on <= payout_cutoff and p.deposit_on <= cal.horizon]
    for p in payouts:
        out.add(
            "payout",
            profile.processor_source,
            _at(p.settled_on),
            p.ref,
            profile.processor_source,
            _cents(p.amount),
            _cents(p.fees),
            p.expected_on.isoformat(),
            p.settled_on.isoformat(),
        )
        out.txns.append(
            _Txn(
                p.deposit_on,
                "in",
                p.deposit_amount,
                profile.processor_name,
                f"{profile.processor_source.upper()} PAYOUT {p.ref}",
            )
        )

    # Wholesale receivables.
    _add_wholesale(out, sales, cal, _rng(seed, "wholesale"))

    # Inventory, supplier invoices, and one-off supplier invoices.
    skus = sku.skus
    supplier_lead = [_rng(seed, f"lead:{i}").randint(5, 10) for i in range(len(SUPPLIERS))]
    supplier_of = [_rng(seed, f"supplier:{name}").randrange(len(SUPPLIERS)) for name in skus]
    base_cost: list[Decimal] = []
    for k, name in enumerate(skus):
        ps = sorted(sku.prices[k])
        median = ps[len(ps) // 2]
        # Each SKU has its own margin: supplier cost is a normally distributed share of its
        # median selling price. Nothing is tuned to a target; the cost-of-goods share of
        # revenue is whatever the orders, stock policy and these costs produce.
        ratio = min(0.68, max(0.32, _rng(seed, f"cost:{name}").gauss(COST_MEAN, COST_SD)))
        base_cost.append((median * Decimal(str(round(ratio, 4)))).quantize(MICRO, ROUND_HALF_UP))
    # Suppliers raise prices in January, each by its own 1-7% a year.
    steps = {
        (i, y): Decimal(str(round(_rng(seed, f"price:{i}:{y}").uniform(1.01, 1.07), 4)))
        for i in range(len(SUPPLIERS))
        for y in range(cal.start.year + 1, cal.horizon.year + 1)
    }

    def cost_on(k: int, day: date) -> Decimal:
        cost = base_cost[k]
        for y in range(cal.start.year + 1, day.year + 1):
            cost *= steps[(supplier_of[k], y)]
        return cost.quantize(MICRO, ROUND_HALF_UP)

    stock = _simulate(sku.units, [supplier_lead[s] for s in supplier_of], cal.start, cal.n_days)
    pay_rng = _rng(seed, "payables")
    purchases = _add_supplier_invoices(out, cal, stock, cost_on, supplier_of, pay_rng, profile)
    oneoff_rng = _rng(seed, "oneoffs")
    for name, code, amount in ONE_OFFS:
        issued = cal.start + timedelta(days=oneoff_rng.randrange(max(1, cal.n_days - 60)))
        _add_payable(out, cal, pay_rng, name, f"PI-{code}-{issued:%Y%m%d}", amount, issued)
        purchases.append((issued, amount))

    aged_candidates = [
        k
        for k in range(len(skus))
        if 0 <= stock.last_sale[k] <= cal.n_days - 1 - AGED_DAYS and stock.on_hand[k] > 0
    ]
    aged_candidates.sort(key=lambda k: (-(stock.on_hand[k] * base_cost[k]), skus[k]))
    ranked = sorted(range(len(skus)), key=lambda k: (-sku.revenue[k], skus[k]))
    chosen = sorted(
        set(ranked[:TOP_SKUS]) | set(aged_candidates[:AGED_SKUS]), key=lambda k: skus[k]
    )
    _add_snapshots(out, cal, skus, stock, cost_on, chosen)

    # Monthly and weekly outflows.
    _add_monthly(
        out,
        cal,
        sales,
        purchases,
        _rng(seed, "payroll"),
        _rng(seed, "ads"),
        _rng(seed, "opex"),
        profile,
    )

    # Planted problems, applied after the clean schedule so they do not shift it.
    if "undisclosed_lender_debit" in plant:
        _plant_lender_debit(out, cal, window("undisclosed_lender_debit"), profile)
    if "self_transfer_revenue" in plant:
        _plant_self_transfer(
            out,
            cal,
            window("self_transfer_revenue"),
            _rng(seed, "plant:self_transfer_revenue"),
            profile,
        )
    if "partial_coverage" in plant:
        _plant_partial_coverage(
            out,
            cal,
            sales,
            window("partial_coverage"),
            _rng(seed, "plant:partial_coverage"),
            profile,
        )

    # Owner loans and drawings, then the daily balance from a fixed opening.
    opening = Decimal(root.randint(120000, 200000))
    _settle_owner(out, cal, opening, profile)
    _add_balances(out, cal, opening, profile)
    _add_bank_rows(out, profile)

    out_dir.mkdir(parents=True, exist_ok=True)
    counts = _write(out_dir, out.entries)
    planted = [
        {
            "name": name,
            "start": window(name).isoformat(),
            "end": cal.horizon.isoformat(),
            "description": _describe(name, profile),
        }
        for name in sorted(plant)
    ]
    (out_dir / "planted.json").write_text(
        json.dumps(planted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "out_dir": str(out_dir),
        "seed": seed,
        "plant": sorted(plant),
        "start": cal.start.isoformat(),
        "horizon": cal.horizon.isoformat(),
        "opening_balance": _cents(opening),
        "rows": counts,
        "planted": planted,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic footprint for the orders.")
    parser.add_argument(
        "--lines",
        type=Path,
        default=Path("data/processed/uci_lines.csv"),
        help="cleaned order lines (default: %(default)s)",
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--plant",
        nargs="*",
        default=[],
        choices=sorted(PLANTS),
        metavar="NAME",
        help=f"planted problems to apply: {', '.join(sorted(PLANTS))}",
    )
    args = parser.parse_args(argv)
    result = generate(args.lines, args.out, seed=args.seed, plant=frozenset(args.plant))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
