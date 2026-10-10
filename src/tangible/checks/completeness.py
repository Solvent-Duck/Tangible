"""Completeness checks: connection coverage, missing periods, stale pulls."""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from tangible.checks.registry import FindingDraft, check
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import BankTxn, Ledger

GAP_WINDOW_DAYS: Final = 90  # time_gaps: look-back for gap days
GAP_RATE_WEEKS: Final = 8  # time_gaps: same-weekday history that sets the expected pattern
GAP_MIN_ACTIVE_RATE: Final = Decimal("0.95")  # share of those weekdays that must have had rows
GAP_LISTED_DAYS: Final = 10  # gap days named in the summary (all are in facts)
STALE_AFTER: Final = timedelta(days=7)  # stale_connection: longest quiet spell tolerated
COVERAGE_WINDOW_DAYS: Final = 90
COVERAGE_MAX_UNCONNECTED_SHARE: Final = Decimal("0.01")
PROCESSOR_KEYWORDS: Final = (
    "SHOPIFY",
    "STRIPE",
    "PAYPAL",
    "SQUARE",
    "AMAZON",
    "EBAY",
    "ETSY",
    "WOOCOMMERCE",
    "KLARNA",
    "TIKTOK",
)
MIN_HISTORY_DAYS: Final = 183  # about six months
REVOKED_LOOKBACK: Final = timedelta(days=30)  # v1 proxy: the case start date is not in the Ledger
MAX_EVIDENCE: Final = 50


def _refs(evidence: Iterable[EvidenceRef | None]) -> tuple[str, ...]:
    unique = dict.fromkeys(str(e) for e in evidence if e is not None)  # keeps order
    return tuple(unique)[:MAX_EVIDENCE]


def _dated_rows(k: Ledger) -> dict[str, list[tuple[date, EvidenceRef | None]]]:
    """Business date of every known row, per source table that has a regular pattern."""
    payouts: list[tuple[date, EvidenceRef | None]] = []
    for p in k.payouts:
        d = p.settled_on or p.expected_on
        if d is not None:
            payouts.append((d, p.evidence))
    return {
        "orders": [(o.occurred_at.date(), o.evidence) for o in k.orders],
        "bank_txns": [(t.posted_on, t.evidence) for t in k.bank_txns],
        "payouts": payouts,
    }


@check("time_gaps", 1)
def time_gaps(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """A business day with no rows where the same weekday was active in >= 95% of the previous
    8 weeks. Only complete days (the day of `as_of` is excluded) before the source's last row are
    judged: silence after the last row is staleness, which stale_connection covers."""
    k = ledger.known(as_of)
    last = as_of.date() - timedelta(days=1)
    drafts: list[FindingDraft] = []
    for table, rows in _dated_rows(k).items():
        if not rows:
            continue
        evidence_by_day: dict[date, EvidenceRef | None] = {}
        for d, ev in rows:
            evidence_by_day.setdefault(d, ev)
        active = sorted(evidence_by_day)
        gaps: list[date] = []
        for i in range(GAP_WINDOW_DAYS):
            d = last - timedelta(days=i)
            if d.weekday() >= 5 or d in evidence_by_day or d > active[-1]:
                continue  # trailing silence after the last row is staleness, not a gap
            prior = [d - timedelta(weeks=w) for w in range(1, GAP_RATE_WEEKS + 1)]
            rate = Decimal(sum(1 for p in prior if p in evidence_by_day)) / GAP_RATE_WEEKS
            if rate >= GAP_MIN_ACTIVE_RATE:
                gaps.append(d)
        if not gaps:
            continue
        gaps.sort()
        refs = _refs(
            evidence_by_day[active[j - 1]]
            for g in gaps
            if (j := bisect_left(active, g)) > 0  # nearest row before the gap
        )
        listed = ", ".join(d.isoformat() for d in gaps[:GAP_LISTED_DAYS])
        more = f" and {len(gaps) - GAP_LISTED_DAYS} more" if len(gaps) > GAP_LISTED_DAYS else ""
        drafts.append(
            FindingDraft(
                check_key="time_gaps",
                check_version=1,
                severity="review",
                pillar="integrity",
                summary=(
                    f"{table} has no rows on {len(gaps)} weekday(s) in the last "
                    f"{GAP_WINDOW_DAYS} days where the previous {GAP_RATE_WEEKS} same weekdays "
                    f"were all active: {listed}{more}."
                ),
                evidence_refs=refs,
                facts={
                    "table": table,
                    "gap_days": ",".join(d.isoformat() for d in gaps),
                    "gap_count": str(len(gaps)),
                    "min_active_rate": str(GAP_MIN_ACTIVE_RATE),
                },
            )
        )
    return drafts


@check("stale_connection", 1)
def stale_connection(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Unrevoked connection with no successful pull for more than 7 days. A connection that has
    never succeeded is measured from when access was granted."""
    k = ledger.known(as_of)
    drafts: list[FindingDraft] = []
    for c in k.connections:
        if c.revoked_at is not None and c.revoked_at <= as_of:
            continue
        since = c.last_success_at or c.granted_at
        age = as_of - since
        if age <= STALE_AFTER:
            continue
        what = "last succeeded" if c.last_success_at else "was granted and has never succeeded"
        drafts.append(
            FindingDraft(
                check_key="stale_connection",
                check_version=1,
                severity="review",
                pillar="integrity",
                summary=(
                    f"Connection to {c.source} {what} at {since.isoformat()}, "
                    f"{age.days} days before the assessment date; the limit is "
                    f"{STALE_AFTER.days} days."
                ),
                evidence_refs=_refs([c.evidence]),
                facts={
                    "source": c.source,
                    "last_success_at": since.isoformat(),
                    "age_days": str(age.days),
                    "max_age_days": str(STALE_AFTER.days),
                },
            )
        )
    return drafts


def _keywords_in(name: str) -> set[str]:
    up = name.upper()
    return {kw for kw in PROCESSOR_KEYWORDS if kw in up}


@check("partial_coverage", 1)
def partial_coverage(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Bank inflows from a known processor/marketplace that is not a connected source.

    Connected sources are the Connection.source values when any connection exists; with none,
    they are inferred from Payout.processor values (payouts are the only other sign of which
    processors are connected).
    """
    k = ledger.known(as_of)
    names = [c.source for c in k.connections] or [p.processor for p in k.payouts]
    connected: set[str] = set()
    for n in names:
        connected |= _keywords_in(n)
    today = as_of.date()
    start = today - timedelta(days=COVERAGE_WINDOW_DAYS)
    inflows = [t for t in k.bank_txns if t.direction == "in" and start < t.posted_on <= today]
    total = sum((t.amount for t in inflows), Decimal(0))
    if total <= 0:
        return []
    by_kw: dict[str, list[BankTxn]] = defaultdict(list)
    for t in inflows:
        for kw in sorted(_keywords_in(t.counterparty or "") - connected):
            by_kw[kw].append(t)
    unconnected = [t for rows in by_kw.values() for t in rows]
    amount = sum((t.amount for t in unconnected), Decimal(0))
    share = amount / total
    if share <= COVERAGE_MAX_UNCONNECTED_SHARE:
        return []
    unconnected.sort(key=lambda t: (-t.amount, t.source_ref))
    per_kw = sorted(by_kw)
    return [
        FindingDraft(
            check_key="partial_coverage",
            check_version=1,
            severity="hold",
            pillar="integrity",
            summary=(
                f"{amount} of {total} in bank inflows over the last {COVERAGE_WINDOW_DAYS} days "
                f"({share:.1%}) came from {', '.join(per_kw)}, which the borrower has not "
                f"connected; the limit is {COVERAGE_MAX_UNCONNECTED_SHARE:.0%}."
            ),
            evidence_refs=_refs(t.evidence for t in unconnected),
            facts={
                "unconnected_inflows": str(amount),
                "total_inflows": str(total),
                "unconnected_share": str(share.quantize(Decimal("0.0001"))),
                "max_share": str(COVERAGE_MAX_UNCONNECTED_SHARE),
                "unconnected_sources": ",".join(per_kw),
                "connected_sources": ",".join(sorted(connected)),
            },
        )
    ]


@check("short_history", 1)
def short_history(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Per source table, fewer than 183 days from its earliest known business date to as_of."""
    k = ledger.known(as_of)
    tables: dict[str, list[tuple[date, EvidenceRef | None]]] = {
        **_dated_rows(k),
        "invoices": [(i.issued_on, i.evidence) for i in k.invoices],
    }
    drafts: list[FindingDraft] = []
    for table, rows in tables.items():
        if not rows:
            continue
        first, ev = min(rows, key=lambda r: r[0])
        days = (as_of.date() - first).days
        if days >= MIN_HISTORY_DAYS:
            continue
        drafts.append(
            FindingDraft(
                check_key="short_history",
                check_version=1,
                severity="info",
                pillar="integrity",
                summary=(
                    f"{table} has {days} days of history (earliest {first.isoformat()}); "
                    f"at least {MIN_HISTORY_DAYS} days (about six months) is expected."
                ),
                evidence_refs=_refs([ev]),
                facts={
                    "table": table,
                    "earliest": first.isoformat(),
                    "history_days": str(days),
                    "min_days": str(MIN_HISTORY_DAYS),
                },
            )
        )
    return drafts


@check("revoked_mid_review", 1)
def revoked_mid_review(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Any connection revoked in the 30 days up to as_of. The case start date is not in the
    Ledger yet, so a 30-day look-back is the v1 proxy for "during the review"."""
    k = ledger.known(as_of)
    hits = sorted(
        (
            (c.revoked_at, c.source_ref, c.source, c.evidence)
            for c in k.connections
            if c.revoked_at is not None and as_of - REVOKED_LOOKBACK <= c.revoked_at <= as_of
        ),
        key=lambda h: (h[0], h[1]),
    )
    if not hits:
        return []
    return [
        FindingDraft(
            check_key="revoked_mid_review",
            check_version=1,
            severity="hold",
            pillar="integrity",
            summary=(
                f"The borrower revoked access to {', '.join(h[2] for h in hits)} within "
                f"{REVOKED_LOOKBACK.days} days before the assessment date."
            ),
            evidence_refs=_refs(h[3] for h in hits),
            facts={
                "revoked_sources": ",".join(h[2] for h in hits),
                "revoked_at": ",".join(h[0].isoformat() for h in hits),
                "lookback_days": str(REVOKED_LOOKBACK.days),
            },
        )
    ]
