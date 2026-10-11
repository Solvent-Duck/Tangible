"""Completeness checks: connection coverage, missing periods, stale pulls."""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from tangible import numfmt
from tangible.checks.calendar_uk import is_business_day
from tangible.checks.registry import FindingDraft, Severity, check
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import BankTxn, Ledger

GAP_WINDOW_DAYS: Final = 90  # time_gaps: look-back for gap days
GAP_RATE_WEEKS: Final = 8  # time_gaps: same-weekday history that sets the expected pattern
GAP_MIN_ACTIVE_RATE: Final = Decimal("0.95")  # share of those weekdays that must have had rows
GAP_LISTED_DAYS: Final = 10  # gap days named in the summary (all are in facts)
GAP_REVIEW_MIN_DAYS: Final = 2  # one quiet day is info (holidays, slow days); two or more is review
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
    judged: silence after the last row is staleness, which stale_connection covers. A single gap
    day is info; GAP_REVIEW_MIN_DAYS or more is review."""
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
                severity="review" if len(gaps) >= GAP_REVIEW_MIN_DAYS else "info",
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
    return _partial_coverage(ledger, as_of, 1, str, lambda x: f"{x:.1%}", lambda x: f"{x:.0%}")


@check("partial_coverage", 2)
def partial_coverage_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check, written in dollars with shares to two decimals (check set cs-7)."""
    return _partial_coverage(ledger, as_of, 2, numfmt.usd, numfmt.pct, numfmt.pct)


def _partial_coverage(
    ledger: Ledger,
    as_of: datetime,
    version: int,
    money: Callable[[Decimal], str],
    share_text: Callable[[Decimal], str],
    limit_text: Callable[[Decimal], str],
) -> list[FindingDraft]:
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
            check_version=version,
            severity="hold",
            pillar="integrity",
            summary=(
                f"{money(amount)} of {money(total)} in bank inflows over the last "
                f"{COVERAGE_WINDOW_DAYS} days ({share_text(share)}) came from "
                f"{', '.join(per_kw)}, which the borrower has not connected; the limit is "
                f"{limit_text(COVERAGE_MAX_UNCONNECTED_SHARE)}."
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


# ---------------------------------------------------------------- time_gaps v2

GAP2_MIN_DAILY_ROWS: Final = Decimal(5)  # at this rate an empty day is < 1% likely by chance
GAP2_PRIOR_MIN_DAYS: Final = 4  # same-weekday business days needed to judge a day
GAP2_REVIEW_P: Final = Decimal("0.001")  # an episode this unlikely by chance is review
GAP2_REVIEW_RUN: Final = 3  # or this many consecutive empty business days (outage)


def _chance(expected: dict[date, Decimal], episode: list[date]) -> Decimal:
    """Poisson chance of no rows on every day of the episode: exp(-sum of daily rates)."""
    return (-sum((expected[d] for d in episode), Decimal(0))).exp()


@check("time_gaps", 2)
def time_gaps_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Missing data, told apart from slow days and holidays.

    Only business days in England and Wales count (weekends and bank holidays are never gaps).
    An empty business day is "unexpected" when the same weekday had rows in every one of the
    previous 8 weeks' business days, or averaged >= GAP2_MIN_DAILY_ROWS rows. Consecutive
    unexpected empty business days form an episode. With r = the summed average daily rows over
    an episode, exp(-r) is the chance of seeing no rows by luck (Poisson). An episode is review
    when that chance is < GAP2_REVIEW_P or it lasts >= GAP2_REVIEW_RUN business days; anything
    less is info. Silence after a source's last row is staleness (stale_connection), not a gap.
    """
    return _time_gaps(ledger, as_of, 2, lambda x: f"{x:.4%}")


@check("time_gaps", 3)
def time_gaps_v3(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v2's check with the chance of no rows to two decimals (check set cs-7)."""
    return _time_gaps(ledger, as_of, 3, numfmt.pct)


def _time_gaps(
    ledger: Ledger, as_of: datetime, version: int, chance_text: Callable[[Decimal], str]
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    last = as_of.date() - timedelta(days=1)
    drafts: list[FindingDraft] = []
    for table, rows in _dated_rows(k).items():
        if not rows:
            continue
        counts: defaultdict[date, int] = defaultdict(int)
        evidence_by_day: dict[date, EvidenceRef | None] = {}
        for d, ev in rows:
            counts[d] += 1
            evidence_by_day.setdefault(d, ev)
        active = sorted(evidence_by_day)
        window = [last - timedelta(days=i) for i in range(GAP_WINDOW_DAYS - 1, -1, -1)]
        days = [d for d in window if is_business_day(d) and d <= active[-1]]
        position = {d: i for i, d in enumerate(days)}
        expected: dict[date, Decimal] = {}
        for d in days:
            if counts.get(d):
                continue
            prior = [
                p
                for w in range(1, GAP_RATE_WEEKS + 1)
                if is_business_day(p := d - timedelta(weeks=w))
            ]
            if len(prior) < GAP2_PRIOR_MIN_DAYS:
                continue
            rate = Decimal(sum(counts.get(p, 0) for p in prior)) / len(prior)
            if rate >= GAP2_MIN_DAILY_ROWS or all(counts.get(p) for p in prior):
                expected[d] = rate
        if not expected:
            continue
        episodes: list[list[date]] = []
        for d in sorted(expected):
            if episodes and position[d] == position[episodes[-1][-1]] + 1:
                episodes[-1].append(d)
            else:
                episodes.append([d])
        chances = [_chance(expected, ep) for ep in episodes]
        serious = [
            c < GAP2_REVIEW_P or len(ep) >= GAP2_REVIEW_RUN
            for c, ep in zip(chances, episodes, strict=True)
        ]
        w = min(
            range(len(episodes)),
            key=lambda i: (not serious[i], chances[i], -len(episodes[i]), episodes[i][0]),
        )
        worst, worst_chance = episodes[w], chances[w]
        gaps = sorted(expected)
        refs = _refs(
            evidence_by_day[active[j - 1]] for g in gaps if (j := bisect_left(active, g)) > 0
        )
        severity: Severity = "review" if any(serious) else "info"
        span = f"{worst[0]}" + (f" to {worst[-1]}" if len(worst) > 1 else "")
        drafts.append(
            FindingDraft(
                check_key="time_gaps",
                check_version=version,
                severity=severity,
                pillar="integrity",
                summary=(
                    f"{table} has no rows on {len(gaps)} business day(s) in the last "
                    f"{GAP_WINDOW_DAYS} days where rows were expected. Worst: {span} "
                    f"({len(worst)} day(s); chance of no rows by luck "
                    f"{chance_text(worst_chance)})."
                ),
                evidence_refs=refs,
                facts={
                    "table": table,
                    "gap_days": ",".join(d.isoformat() for d in gaps),
                    "gap_count": str(len(gaps)),
                    "episodes": str(len(episodes)),
                    "worst_episode": ",".join(d.isoformat() for d in worst),
                    "worst_chance": str(worst_chance.quantize(Decimal("0.000001"))),
                    "review_p": str(GAP2_REVIEW_P),
                    "review_run": str(GAP2_REVIEW_RUN),
                },
            )
        )
    return drafts
