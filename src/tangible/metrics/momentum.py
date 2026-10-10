"""Momentum metrics: which way and how fast (30v30 change, 13-week slope, 26-week breaks).

All are v1, return Decimal | None, and read rows only through `ledger.known(as_of)`.
Changes are relative ((now - prior) / |prior|); slopes are fractions of the series mean per
week; None means "not judgeable" (no data, zero denominator, too little history).

Windows are the trailing N days ending at as_of: (as_of - N, as_of], on occurred_at after the
time rule has been applied. Bank transactions only have a posted date, taken as midnight in
as_of's timezone.

Health-metric momentum: `register_momentum_for(key, version)` registers
`<key>_change_30v30` and `<key>_slope_13w` for any registered (health) metric. It is NOT
called here; the coordinator calls it after merge for payout_lag, days_of_inventory and
payables_days. Because those metrics already span 90-day windows, a 30v30 comparison of the
metric itself would be meaningless, so the factory compares the metric's latest value with its
value 30 days earlier, and takes the slope over 13 weekly points of the metric (normalised by
the series mean, so it is a fraction per week).
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from tangible.ledger.types import BankTxn, Ledger
from tangible.metrics import series
from tangible.metrics.registry import REGISTRY, MetricFn, metric

# Bank outflows whose counterparty contains one of these (case-insensitive) count as ad spend.
AD_PLATFORMS: tuple[str, ...] = (
    "GOOGLE ADS",
    "META ADS",
    "FACEBOOK",
    "TIKTOK ADS",
    "AMAZON ADS",
    "MICROSOFT ADS",
    "PINTEREST",
)

REVENUE_SERIES_DAYS = 30  # each weekly point is the trailing-30-day net revenue
YOY_DAYS = 90


def _net_revenue(known: Ledger, end: datetime, days: int, back: int = 0) -> Decimal:
    gross = series.window_sum(
        known.orders, lambda o: o.occurred_at, lambda o: o.gross - o.discounts, end, days, back
    )
    refunds = series.window_sum(
        known.refunds, lambda r: r.occurred_at, lambda r: r.amount, end, days, back
    )
    return gross - refunds


def _refund_rate(known: Ledger, end: datetime, days: int, back: int = 0) -> Decimal | None:
    gross = series.window_sum(
        known.orders, lambda o: o.occurred_at, lambda o: o.gross, end, days, back
    )
    if gross == 0:
        return None
    refunds = series.window_sum(
        known.refunds, lambda r: r.occurred_at, lambda r: r.amount, end, days, back
    )
    return refunds / gross


def _is_ad_spend(t: BankTxn) -> bool:
    cp = (t.counterparty or "").upper()
    return t.direction == "out" and any(p in cp for p in AD_PLATFORMS)


def _ad_ratio(known: Ledger, end: datetime, days: int, back: int = 0) -> Decimal | None:
    net = _net_revenue(known, end, days, back)
    if net <= 0:
        return None
    tz = end.tzinfo or UTC
    ads = series.window_sum(
        (t for t in known.bank_txns if _is_ad_spend(t)),
        lambda t: datetime.combine(t.posted_on, time.min, tzinfo=tz),
        lambda t: t.amount,
        end,
        days,
        back,
    )
    return ads / net


@metric("revenue_growth_30v30", 1)
def revenue_growth_30v30(ledger: Ledger, as_of: datetime) -> Decimal | None:
    known = ledger.known(as_of)
    return series.change(_net_revenue(known, as_of, 30), _net_revenue(known, as_of, 30, back=30))


def _revenue_30d(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Trailing-30-day net revenue at `as_of` (series point function; applies the time rule)."""
    return _net_revenue(ledger.known(as_of), as_of, REVENUE_SERIES_DAYS)


@metric("revenue_slope_13w", 1)
def revenue_slope_13w(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Slope of 13 weekly trailing-30-day net revenue points, as a fraction of their mean/week."""
    pts = series.weekly_series(_revenue_30d, ledger, as_of, 13)
    return series.normalised_slope(series.values(pts))


@metric("revenue_break_26w", 1)
def revenue_break_26w(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """1 if the latest weekly 30-day revenue is outside mean +/- 2 sigma of the 25 weeks before.

    Weekly points of a 30-day window overlap, so the baseline is smoother than independent
    samples; None when fewer than 8 usable baseline points or zero variance.
    """
    pts = series.values(series.weekly_series(_revenue_30d, ledger, as_of, 26))
    broke = series.is_break(pts[:-1], pts[-1])
    return None if broke is None else Decimal(int(broke))


@metric("refund_rate_change_30v30", 1)
def refund_rate_change_30v30(ledger: Ledger, as_of: datetime) -> Decimal | None:
    known = ledger.known(as_of)
    return series.change(_refund_rate(known, as_of, 30), _refund_rate(known, as_of, 30, back=30))


@metric("ad_spend_ratio_change_30v30", 1)
def ad_spend_ratio_change_30v30(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Relative change in (ad spend / net revenue), last 30 days vs the 30 before.

    None when no ad spend in the prior window (prior ratio 0) or net revenue <= 0.
    """
    known = ledger.known(as_of)
    return series.change(_ad_ratio(known, as_of, 30), _ad_ratio(known, as_of, 30, back=30))


@metric("revenue_yoy_90d", 1)
def revenue_yoy_90d(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Trailing-90d net revenue vs the same 90 days a year (365 days) earlier, relative change.

    None unless the known orders reach back to the start of the year-ago window
    (>= 455 days of history); a partly-covered comparison window would understate it.
    """
    known = ledger.known(as_of)
    if not known.orders:
        return None
    earliest = min(o.occurred_at for o in known.orders)
    if earliest > as_of - timedelta(days=365 + YOY_DAYS):
        return None
    return series.change(
        _net_revenue(known, as_of, YOY_DAYS), _net_revenue(known, as_of, YOY_DAYS, back=365)
    )


def register_momentum_for(key: str, version: int) -> tuple[str, str]:
    """Register `<key>_change_30v30` and `<key>_slope_13w` (both v1) over registered metric `key`.

    change: relative change between the metric's value at as_of and at as_of - 30 days.
    slope: normalised least-squares slope over 13 weekly points of the metric.
    Raises KeyError if (key, version) is not registered. Returns the two new keys.
    """
    base: MetricFn = REGISTRY[(key, version)].fn

    def change_fn(ledger: Ledger, as_of: datetime) -> Decimal | None:
        return series.change(base(ledger, as_of), base(ledger, as_of - timedelta(days=30)))

    def slope_fn(ledger: Ledger, as_of: datetime) -> Decimal | None:
        pts = series.weekly_series(base, ledger, as_of, 13)
        return series.normalised_slope(series.values(pts))

    change_key, slope_key = f"{key}_change_30v30", f"{key}_slope_13w"
    metric(change_key, 1)(change_fn)
    metric(slope_key, 1)(slope_fn)
    return change_key, slope_key
