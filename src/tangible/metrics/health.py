"""Health metrics (trailing 90 days unless noted)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from tangible.ledger.types import (
    AccountBalance,
    InventorySnapshot,
    Ledger,
    Order,
    Refund,
    known_as_of,
)
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


# --- Health metrics added in WP1B. All start from `ledger.known(as_of)` (the time rule) and then
# filter on business dates; the trailing window is the business date in (as_of - 90d, as_of].

TRADE_CREDIT_DAYS = 21  # v1 heuristic: payables with terms >= this are treated as stock purchases
_ZERO = Decimal(0)


def _bounds(as_of: datetime) -> tuple[date, date]:
    end = as_of.date()
    return end - WINDOW, end


def _net_revenue_known(k: Ledger, as_of: datetime) -> Decimal:
    start = as_of - WINDOW
    gross = sum((o.gross - o.discounts for o in k.orders if start < o.occurred_at <= as_of), _ZERO)
    refunds = sum((r.amount for r in k.refunds if start < r.occurred_at <= as_of), _ZERO)
    return gross - refunds


def _stock_value(k: Ledger, on_or_before: date) -> Decimal | None:
    """Sum of units x unit_cost over the latest snapshot per SKU dated <= `on_or_before`.

    Snapshots without a unit cost are skipped. None when no costed snapshot exists.
    """
    latest: dict[str, InventorySnapshot] = {}
    for s in k.inventory_snapshots:
        if s.as_of <= on_or_before and (s.sku not in latest or s.as_of >= latest[s.sku].as_of):
            latest[s.sku] = s
    costed = [s.units_on_hand * s.unit_cost for s in latest.values() if s.unit_cost is not None]
    if not costed:
        return None
    return sum(costed, _ZERO)


def _cogs(k: Ledger, as_of: datetime) -> Decimal | None:
    start, end = _bounds(as_of)
    opening = _stock_value(k, start)
    closing = _stock_value(k, end)
    if opening is None or closing is None:
        return None
    purchases = sum(
        (
            i.amount
            for i in k.invoices
            if i.kind == "payable"
            and i.due_on is not None
            and start < i.issued_on <= end
            and (i.due_on - i.issued_on).days >= TRADE_CREDIT_DAYS
        ),
        _ZERO,
    )
    return opening + purchases - closing


@metric("gross_margin_est", 1)
def gross_margin_est(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """(net revenue - COGS) / net revenue over the trailing 90 days.

    COGS = opening stock value + purchases - closing stock value. Stock value is the sum of
    units_on_hand x unit_cost of the latest snapshot per SKU dated at/before the window start
    (opening) or at/before as_of (closing). Purchases are payable invoices issued in the window
    whose terms (due_on - issued_on) are >= 21 days. ASSUMPTION (v1): the schema has no invoice
    category, so long trade-credit terms stand in for "stock supplier"; short-term opex payables
    (ads, software, utilities) are excluded. Credit notes (negative amounts) are included as is.
    None when stock snapshots (opening or closing) are missing or net revenue is zero.
    """
    k = ledger.known(as_of)
    cogs = _cogs(k, as_of)
    revenue = _net_revenue_known(k, as_of)
    if cogs is None or revenue == 0:
        return None
    return (revenue - cogs) / revenue


@metric("repeat_customer_share", 1)
def repeat_customer_share(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Revenue from customers with >= 2 orders in the window / window net revenue.

    Numerator revenue is gross - discounts of those customers' orders (refunds are not
    attributed to customers). Anonymous orders (no customer_ref) count only in the denominator,
    which is net revenue as in `net_revenue` (after refunds). None when net revenue is zero.
    """
    k = ledger.known(as_of)
    start = as_of - WINDOW
    by_customer: dict[str, list[Decimal]] = {}
    for o in k.orders:
        if start < o.occurred_at <= as_of and o.customer_ref is not None:
            by_customer.setdefault(o.customer_ref, []).append(o.gross - o.discounts)
    revenue = _net_revenue_known(k, as_of)
    if revenue == 0:
        return None
    repeat = sum((sum(v, _ZERO) for v in by_customer.values() if len(v) >= 2), _ZERO)
    return repeat / revenue


@metric("days_of_inventory", 1)
def days_of_inventory(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Closing stock value / (COGS / 90).

    Value-based because orders carry no SKU lines, so units sold cannot be computed. COGS is as
    in `gross_margin_est` (including its trade-credit purchase heuristic). None when stock is
    missing or COGS is not positive.
    """
    k = ledger.known(as_of)
    cogs = _cogs(k, as_of)
    closing = _stock_value(k, as_of.date())
    if cogs is None or closing is None or cogs <= 0:
        return None
    return closing / (cogs / 90)


@metric("aged_stock_share", 1)
def aged_stock_share(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Closing value of SKUs with no sale in the window / total closing stock value.

    A SKU is "aged" when its units_on_hand never decreased between consecutive snapshots dated in
    (as_of - 90d, as_of]. A SKU needs >= 2 snapshots in the window to be judged; others are not
    aged but still count in the denominator. None when no SKU can be judged or total value is 0.
    """
    k = ledger.known(as_of)
    start, end = _bounds(as_of)
    by_sku: dict[str, list[InventorySnapshot]] = {}
    for s in k.inventory_snapshots:
        if s.as_of <= end:
            by_sku.setdefault(s.sku, []).append(s)
    total = _ZERO
    aged = _ZERO
    judged = 0
    for snaps in by_sku.values():
        snaps.sort(key=lambda s: (s.as_of, s.observed_at))
        last = snaps[-1]
        if last.unit_cost is None:
            continue
        value = last.units_on_hand * last.unit_cost
        total += value
        recent = [s for s in snaps if s.as_of > start]
        if len(recent) < 2:
            continue
        judged += 1
        if all(
            b.units_on_hand >= a.units_on_hand for a, b in zip(recent, recent[1:], strict=False)
        ):
            aged += value
    if judged == 0 or total == 0:
        return None
    return aged / total


@metric("cash_runway", 1)
def cash_runway(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Latest balance / average monthly net outflow, in months.

    Balance is the sum over accounts of the latest balance dated <= as_of. Net outflow is
    -(sum of signed bank rows posted in the trailing 90 days) and the monthly average is that / 3.
    Returns None when the business is not burning cash (net flow >= 0), or inputs are missing;
    policy treats None as "not evaluated", not as a failure.
    """
    k = ledger.known(as_of)
    start, end = _bounds(as_of)
    latest: dict[str, AccountBalance] = {}
    for b in k.account_balances:
        if b.as_of <= end and (
            b.account_ref not in latest or b.as_of >= latest[b.account_ref].as_of
        ):
            latest[b.account_ref] = b
    rows = [t for t in k.bank_txns if start < t.posted_on <= end]
    if not latest or not rows:
        return None
    net = sum((t.signed for t in rows), _ZERO)
    if net >= 0:
        return None
    balance = sum((b.balance for b in latest.values()), _ZERO)
    return balance / (-net / 3)


@metric("payables_discipline", 1)
def payables_discipline(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Share of payable invoices paid on or before due_on.

    Population: positive-amount payables with due_on in (as_of - 90d, as_of] that are either paid
    (paid_on <= as_of) or past due at as_of (due_on < as_of date). Unpaid past-due counts as late.
    Credit notes are excluded. None when the population is empty.
    """
    k = ledger.known(as_of)
    start, end = _bounds(as_of)
    total = ontime = 0
    for i in k.invoices:
        if i.kind != "payable" or i.amount <= 0 or i.due_on is None:
            continue
        if not start < i.due_on <= end:
            continue
        paid_on = i.paid_on if i.paid_on is not None and i.paid_on <= end else None
        if paid_on is None and i.due_on >= end:
            continue
        total += 1
        if paid_on is not None and paid_on <= i.due_on:
            ontime += 1
    if total == 0:
        return None
    return Decimal(ontime) / Decimal(total)


@metric("payout_lag", 1)
def payout_lag(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Median of (settled_on - expected_on) in days over payouts settled in the window.

    Proxy: payouts do not link to orders, so this measures processor settlement delay against
    the expected date rather than order-to-cash. Payouts missing either date are ignored. For an
    even count the median is the mean of the two middle values. None when there are none.
    """
    k = ledger.known(as_of)
    start, end = _bounds(as_of)
    lags = sorted(
        (p.settled_on - p.expected_on).days
        for p in k.payouts
        if p.settled_on is not None and p.expected_on is not None and start < p.settled_on <= end
    )
    if not lags:
        return None
    n = len(lags)
    mid = n // 2
    if n % 2:
        return Decimal(lags[mid])
    return Decimal(lags[mid - 1] + lags[mid]) / 2


@metric("history_days", 1)
def history_days(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Whole days from the earliest known order's occurred_at to as_of (for policy history caps).

    Only orders known and occurring at/before as_of count. None when there are none.
    """
    k = ledger.known(as_of)
    times = [o.occurred_at for o in k.orders if o.occurred_at <= as_of]
    if not times:
        return None
    return Decimal((as_of - min(times)).days)


@metric("payables_days", 1)
def payables_days(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Average days from issued_on to paid_on for positive payables paid in the window.

    None when no payable was paid in (as_of - 90d, as_of].
    """
    k = ledger.known(as_of)
    start, end = _bounds(as_of)
    days = [
        (i.paid_on - i.issued_on).days
        for i in k.invoices
        if i.kind == "payable"
        and i.amount > 0
        and i.paid_on is not None
        and start < i.paid_on <= end
    ]
    if not days:
        return None
    return Decimal(sum(days)) / len(days)
