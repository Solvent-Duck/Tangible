"""Reconciliation checks: orders vs payouts vs bank deposits vs stated revenue.

Every check is a pure function of (ledger, as_of). Each starts with `ledger.known(as_of)` (the
time rule), looks at the trailing 90 days by business date, and returns [] when the source it
needs is absent: missing data is the completeness checks' job. Thresholds are module constants;
changing one means a new check version, never an edit to a released one.
"""

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from tangible.checks.registry import FindingDraft, Severity, check
from tangible.ledger.types import BankTxn, InventorySnapshot, Invoice, Ledger, Order, Payout, Refund

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
WINDOW_DAYS: Final = 90
MAX_EVIDENCE: Final = 50
PILLAR: Final = "integrity"

# payouts_vs_deposits v1
MATCH_DAYS: Final = 3
MATCH_TOLERANCE: Final = Decimal("0.01")
MATCHED_REVIEW_BELOW: Final = Decimal("0.95")
MATCHED_HOLD_BELOW: Final = Decimal("0.85")

# books_vs_processor v1
BOOKS_GAP_REVIEW: Final = Decimal("0.05")
BOOKS_GAP_HOLD: Final = Decimal("0.15")

# orders_vs_payouts v1
# v1 heuristic: orders carry no channel in the synthetic data, so an order whose net is under
# this is treated as a card order (the synthetic generator sends GBP 500+ orders to invoices).
CARD_ORDER_MAX_NET: Final = Decimal(500)
ORDERS_GAP_REVIEW: Final = Decimal("0.05")
# A payout expected on day E is settled up to 4 days later (2 business days over a weekend),
# so payouts expected in the last days of the window are not known yet.
PAYOUT_LAG_DAYS: Final = 4

# inventory_flow v1
JUMP_FACTOR: Final = Decimal(10)
JUMP_MIN_UNITS: Final = Decimal(50)  # ignore small absolute moves, however large the ratio
PURCHASE_MIN_TERMS_DAYS: Final = 21  # supplier purchases are net-30; utilities and fees are not
INVOICE_LAG_DAYS: Final = 7  # stock received in a week is invoiced the Monday after

# payables_vs_bank v1
PAYABLE_REVIEW_MISSING: Final = Decimal(1000)

# stated_vs_observed v1
STATED_OVER_REVIEW: Final = Decimal("0.10")


# helpers


def _window(as_of: datetime) -> tuple[date, date]:
    """(start, end): business dates start <= d < end, the 90 days before the as_of date."""
    end = as_of.date()
    return end - timedelta(days=WINDOW_DAYS), end


def _q4(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.0001')):f}"


def _q2(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01')):f}"


def _gbp(x: Decimal) -> str:
    return f"GBP {x:,.2f}"


def _pct(x: Decimal) -> str:
    return f"{(x * 100):.1f}%"


def _refs(rows: Iterable[tuple[Decimal, str, object]]) -> tuple[str, ...]:
    """Evidence refs for (weight, tiebreak, row-with-evidence) triples, most material first."""
    out: list[str] = []
    for _, _, row in sorted(rows, key=lambda t: (-t[0], t[1])):
        ev = getattr(row, "evidence", None)
        if ev is not None:
            out.append(str(ev))
        if len(out) >= MAX_EVIDENCE:
            break
    return tuple(out)


def _order_net(o: Order) -> Decimal:
    return o.gross - o.discounts


def _orders_in(orders: Iterable[Order], start: date, end: date) -> list[Order]:
    return [o for o in orders if start <= o.occurred_at.date() < end]


def _refunds_in(refunds: Iterable[Refund], start: date, end: date) -> list[Refund]:
    return [r for r in refunds if start <= r.occurred_at.date() < end]


def _finding(
    key: str,
    severity: Severity,
    summary: str,
    refs: tuple[str, ...],
    facts: dict[str, str],
) -> FindingDraft:
    return FindingDraft(
        check_key=key,
        check_version=1,
        severity=severity,
        pillar=PILLAR,
        summary=summary,
        evidence_refs=refs,
        facts=facts,
    )


# 1. payouts vs deposits


def _match_payouts(
    payouts: list[Payout], deposits: list[BankTxn]
) -> tuple[dict[str, BankTxn], list[Payout]]:
    """Greedy one-to-one: payouts in (settled_on, amount desc, ref) order each take the best free
    deposit (prefers one naming the payout, then closest amount, then closest day)."""
    free = sorted(deposits, key=lambda t: (t.posted_on, t.source_ref))
    used: set[str] = set()
    matched: dict[str, BankTxn] = {}
    unmatched: list[Payout] = []
    for p in sorted(payouts, key=lambda p: (p.settled_on or date.min, -p.amount, p.source_ref)):
        assert p.settled_on is not None
        tol = p.amount * MATCH_TOLERANCE
        best: tuple[int, Decimal, int, str] | None = None
        best_t: BankTxn | None = None
        for t in free:
            if t.source_ref in used:
                continue
            days = abs((t.posted_on - p.settled_on).days)
            diff = abs(t.amount - p.amount)
            if days > MATCH_DAYS or diff > tol:
                continue
            names_it = 0 if t.description and p.source_ref in t.description else 1
            rank = (names_it, diff, days, t.source_ref)
            if best is None or rank < best:
                best, best_t = rank, t
        if best_t is None:
            unmatched.append(p)
        else:
            used.add(best_t.source_ref)
            matched[p.source_ref] = best_t
    return matched, unmatched


@check("payouts_vs_deposits", 1)
def payouts_vs_deposits(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    k = ledger.known(as_of)
    start, end = _window(as_of)
    # Only payouts old enough that their deposit could already have posted and been seen.
    mature = end - timedelta(days=MATCH_DAYS)
    payouts = [
        p
        for p in k.payouts
        if p.settled_on is not None and start <= p.settled_on < mature and p.amount > 0
    ]
    if not payouts:
        return []
    deposits = [t for t in k.bank_txns if t.direction == "in"]
    _, unmatched = _match_payouts(payouts, deposits)
    total = sum((p.amount for p in payouts), ZERO)
    missing = sum((p.amount for p in unmatched), ZERO)
    share = (total - missing) / total
    if share >= MATCHED_REVIEW_BELOW:
        return []
    severity: Severity = "hold" if share < MATCHED_HOLD_BELOW else "review"
    summary = (
        f"Only {_pct(share)} of settled payout value ({_gbp(total - missing)} of {_gbp(total)}) "
        f"has a matching bank deposit within {MATCH_DAYS} days and 1%; {len(unmatched)} of "
        f"{len(payouts)} payouts worth {_gbp(missing)} have no deposit."
    )
    return [
        _finding(
            "payouts_vs_deposits",
            severity,
            summary,
            _refs((p.amount, p.source_ref, p) for p in unmatched),
            {
                "matched_share": _q4(share),
                "threshold_review": str(MATCHED_REVIEW_BELOW),
                "threshold_hold": str(MATCHED_HOLD_BELOW),
                "payout_total": _q2(total),
                "unmatched_total": _q2(missing),
                "payouts": str(len(payouts)),
                "unmatched_payouts": str(len(unmatched)),
            },
        )
    ]


# 2. books vs processor


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _full_months(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar months [first, next_first) wholly inside [start, end)."""
    out: list[tuple[date, date]] = []
    m = _month_start(start)
    if m < start:
        m = _next_month(m)
    while _next_month(m) <= end:
        out.append((m, _next_month(m)))
        m = _next_month(m)
    return out


def _sales_day(p: Payout) -> date:
    """The last day of sales a payout covers: it is expected the day after."""
    if p.expected_on is not None:
        return p.expected_on - timedelta(days=1)
    assert p.settled_on is not None
    return p.settled_on


@check("books_vs_processor", 1)
def books_vs_processor(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    k = ledger.known(as_of)
    start, end = _window(as_of)
    payouts = [p for p in k.payouts if p.settled_on is not None]
    if not payouts or not k.orders:
        return []
    # v1 definition note: a payout is attributed to the month of the sales it covers (the day
    # before its expected_on), not the month it settled. On clean synthetic data, settled-month
    # attribution leaves a 7-8% timing gap (settlement lags sales by 2-4 days and daily sales are
    # lumpy), which would put every clean merchant in review. Attributing by sale date leaves ~2%.
    # A month is judged only once its last payouts could have settled and been seen.
    receivables = [i for i in k.invoices if i.kind == "receivable"]
    worst: tuple[Decimal, date, Decimal, Decimal, list[Payout], list[Invoice]] | None = None
    for first, nxt in _full_months(start, end):
        if nxt + timedelta(days=PAYOUT_LAG_DAYS) > end:
            continue
        books = sum((_order_net(o) for o in _orders_in(k.orders, first, nxt)), ZERO) - sum(
            (r.amount for r in _refunds_in(k.refunds, first, nxt)), ZERO
        )
        if books <= 0:
            continue
        m_payouts = [p for p in payouts if first <= _sales_day(p) < nxt]
        m_invoices = [i for i in receivables if first <= i.issued_on < nxt]
        processor = sum((p.amount + p.fees for p in m_payouts), ZERO) + sum(
            (i.amount for i in m_invoices), ZERO
        )
        gap = abs(books - processor) / books
        if worst is None or gap > worst[0]:
            worst = (gap, first, books, processor, m_payouts, m_invoices)
    if worst is None or worst[0] <= BOOKS_GAP_REVIEW:
        return []
    gap, first, books, processor, m_payouts, m_invoices = worst
    severity: Severity = "hold" if gap > BOOKS_GAP_HOLD else "review"
    direction = "above" if processor > books else "below"
    summary = (
        f"In {first:%B %Y} the books show {_gbp(books)} of net revenue but the processor and "
        f"invoice records show {_gbp(processor)} ({_pct(gap)} {direction} the books)."
    )
    weighted: list[tuple[Decimal, str, object]] = [
        (abs(p.amount + p.fees), p.source_ref, p) for p in m_payouts
    ] + [(abs(i.amount), i.source_ref, i) for i in m_invoices]
    return [
        _finding(
            "books_vs_processor",
            severity,
            summary,
            _refs(weighted),
            {
                "month": f"{first:%Y-%m}",
                "books_net": _q2(books),
                "processor_total": _q2(processor),
                "gap": _q4(gap),
                "threshold_review": str(BOOKS_GAP_REVIEW),
                "threshold_hold": str(BOOKS_GAP_HOLD),
            },
        )
    ]


# 3. orders vs payouts


@check("orders_vs_payouts", 1)
def orders_vs_payouts(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    k = ledger.known(as_of)
    start, end = _window(as_of)
    last_expected = end - timedelta(days=PAYOUT_LAG_DAYS)
    candidates = sorted(
        (
            p
            for p in k.payouts
            if p.expected_on is not None and start <= p.expected_on <= last_expected
        ),
        key=lambda p: (p.expected_on or date.min, p.source_ref),
    )
    if len(candidates) < 2 or not k.orders:
        return []
    # A payout expected on E covers card sales from the previous payout up to E-1, so payouts
    # (first, last] cover orders [first, last). This keeps the two sides on the same days.
    first_e = candidates[0].expected_on
    last_e = candidates[-1].expected_on
    assert first_e is not None and last_e is not None
    covered = [p for p in candidates if p.expected_on != first_e]
    orders = [
        o for o in _orders_in(k.orders, first_e, last_e) if _order_net(o) < CARD_ORDER_MAX_NET
    ]
    refunds = [r for r in _refunds_in(k.refunds, first_e, last_e) if r.amount < CARD_ORDER_MAX_NET]
    card_net = sum((_order_net(o) for o in orders), ZERO) - sum((r.amount for r in refunds), ZERO)
    paid = sum((p.amount + p.fees for p in covered), ZERO)
    if card_net <= 0:
        return []
    gap = abs(card_net - paid) / card_net
    if gap <= ORDERS_GAP_REVIEW:
        return []
    direction = "more" if paid > card_net else "less"
    summary = (
        f"Card-sized orders from {first_e} to {last_e} net {_gbp(card_net)}, but processor "
        f"payouts plus fees for the same days total {_gbp(paid)}: {_pct(gap)} {direction} than "
        f"the orders explain."
    )
    return [
        _finding(
            "orders_vs_payouts",
            "review",
            summary,
            _refs((abs(p.amount + p.fees), p.source_ref, p) for p in covered),
            {
                "card_order_net": _q2(card_net),
                "payouts_plus_fees": _q2(paid),
                "gap": _q4(gap),
                "threshold_review": str(ORDERS_GAP_REVIEW),
                "card_order_max_net": str(CARD_ORDER_MAX_NET),
            },
        )
    ]


# 4. inventory flow


@check("inventory_flow", 1)
def inventory_flow(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1 is a plausibility test, not a unit reconciliation.

    A real flow test (opening + purchased - sold ~ closing) needs units purchased and units sold
    per SKU; the ledger has neither (supplier invoices are per supplier per week, orders carry no
    SKU). So v1 flags (a) negative units on hand and (b) a SKU whose stock jumps more than 10x
    between snapshots with no supplier purchase invoice (net terms of 21+ days) issued around
    that time. The gap is honest: stock that vanishes or appears in believable amounts is not seen.
    """
    k = ledger.known(as_of)
    start, end = _window(as_of)
    snaps = [s for s in k.inventory_snapshots if start <= s.as_of < end]
    if not snaps:
        return []
    purchases = [
        i
        for i in k.invoices
        if i.kind == "payable"
        and i.due_on is not None
        and (i.due_on - i.issued_on).days >= PURCHASE_MIN_TERMS_DAYS
    ]
    negatives = [s for s in snaps if s.units_on_hand < 0]
    by_sku: dict[str, list[InventorySnapshot]] = {}
    for s in sorted(snaps, key=lambda s: (s.sku, s.as_of, s.source_ref)):
        by_sku.setdefault(s.sku, []).append(s)
    jumps: list[InventorySnapshot] = []
    judged_until = end - timedelta(days=INVOICE_LAG_DAYS)  # later jumps' invoices may not exist yet
    for series in by_sku.values():
        for prev, cur in zip(series, series[1:], strict=False):
            if cur.as_of > judged_until or cur.units_on_hand - prev.units_on_hand < JUMP_MIN_UNITS:
                continue
            if cur.units_on_hand <= JUMP_FACTOR * max(prev.units_on_hand, ONE):
                continue
            lo = prev.as_of
            hi = cur.as_of + timedelta(days=INVOICE_LAG_DAYS)
            if not any(lo <= i.issued_on <= hi for i in purchases):
                jumps.append(cur)
    if not negatives and not jumps:
        return []
    parts: list[str] = []
    if negatives:
        parts.append(f"{len({s.sku for s in negatives})} SKUs show negative units on hand")
    if jumps:
        parts.append(
            f"{len({s.sku for s in jumps})} SKUs jumped more than {JUMP_FACTOR}x between "
            "snapshots with no supplier purchase invoice around that time"
        )
    summary = "Stock records do not hang together: " + "; ".join(parts) + "."
    weighted = [(-s.units_on_hand, s.source_ref, s) for s in negatives] + [
        (s.units_on_hand, s.source_ref, s) for s in jumps
    ]
    return [
        _finding(
            "inventory_flow",
            "review",
            summary,
            _refs(weighted),
            {
                "negative_snapshots": str(len(negatives)),
                "unexplained_jumps": str(len(jumps)),
                "jump_factor": str(JUMP_FACTOR),
                "jump_min_units": str(JUMP_MIN_UNITS),
                "purchase_min_terms_days": str(PURCHASE_MIN_TERMS_DAYS),
            },
        )
    ]


# 5. payables vs bank


def _same_party(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


@check("payables_vs_bank", 1)
def payables_vs_bank(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    k = ledger.known(as_of)
    start, end = _window(as_of)
    paid = [
        i
        for i in k.invoices
        if i.kind == "payable" and i.paid_on is not None and start <= i.paid_on < end
    ]
    if not paid or not k.bank_txns:
        return []
    outflows = sorted(
        (t for t in k.bank_txns if t.direction == "out"), key=lambda t: (t.posted_on, t.source_ref)
    )
    used: set[str] = set()
    missing: list[Invoice] = []
    for inv in sorted(paid, key=lambda i: (i.paid_on or date.min, -i.amount, i.source_ref)):
        assert inv.paid_on is not None
        tol = inv.amount * MATCH_TOLERANCE
        best: tuple[int, Decimal, int, str] | None = None
        best_t: BankTxn | None = None
        for t in outflows:
            if t.source_ref in used or not _same_party(t.counterparty, inv.counterparty):
                continue
            days = abs((t.posted_on - inv.paid_on).days)
            diff = abs(t.amount - inv.amount)
            if days > MATCH_DAYS or diff > tol:
                continue
            names_it = 0 if t.description and inv.source_ref in t.description else 1
            rank = (names_it, diff, days, t.source_ref)
            if best is None or rank < best:
                best, best_t = rank, t
        if best_t is None:
            missing.append(inv)
        else:
            used.add(best_t.source_ref)
    if not missing:
        return []
    total = sum((i.amount for i in missing), ZERO)
    severity: Severity = "review" if total >= PAYABLE_REVIEW_MISSING else "info"
    summary = (
        f"{len(missing)} of {len(paid)} payables marked paid have no matching bank payment to "
        f"the same supplier within {MATCH_DAYS} days and 1%, worth {_gbp(total)} in total."
    )
    return [
        _finding(
            "payables_vs_bank",
            severity,
            summary,
            _refs((i.amount, i.source_ref, i) for i in missing),
            {
                "missing_total": _q2(total),
                "missing_count": str(len(missing)),
                "paid_count": str(len(paid)),
                "threshold_review": str(PAYABLE_REVIEW_MISSING),
            },
        )
    ]


# 6. stated vs observed


@check("stated_vs_observed", 1)
def stated_vs_observed(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    k = ledger.known(as_of)
    app = k.application
    if app is None or app.stated_revenue_90d is None:
        return []
    start, end = _window(as_of)
    orders = _orders_in(k.orders, start, end)
    refunds = _refunds_in(k.refunds, start, end)
    observed = sum((_order_net(o) for o in orders), ZERO) - sum((r.amount for r in refunds), ZERO)
    stated = app.stated_revenue_90d
    if observed > 0:
        over = (stated - observed) / observed
        if over <= STATED_OVER_REVIEW:
            return []
        over_text = f"{_pct(over)} higher"
        over_fact = _q4(over)
    else:
        if stated <= 0:
            return []
        over_text = "with no observed revenue to compare"
        over_fact = "n/a"
    summary = (
        f"The application states {_gbp(stated)} of revenue for 90 days, but connected data shows "
        f"{_gbp(observed)} net from {start} to {end}: stated is {over_text}."
    )
    weighted: list[tuple[Decimal, str, object]] = [(Decimal("Infinity"), app.source_ref, app)] + [
        (_order_net(o), o.source_ref, o) for o in orders
    ]
    return [
        _finding(
            "stated_vs_observed",
            "review",
            summary,
            _refs(weighted),
            {
                "stated_revenue_90d": _q2(stated),
                "observed_revenue_90d": _q2(observed),
                "stated_over_observed": over_fact,
                "threshold_review": str(STATED_OVER_REVIEW),
            },
        )
    ]
