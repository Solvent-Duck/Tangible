"""Series helpers: weekly sampling, slope, relative change, range breaks. Decimal only.

Time rule: `weekly_series` evaluates the metric function independently at each point's own
date, so every point sees only rows observed by that date (no look-ahead). Never compute a
series by slicing a single as_of snapshot.

Series values passed to `slope` / `is_break` are plain `Decimal | None` sequences (use
`values()` to strip the timestamps from a `weekly_series` result).
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from tangible.ledger.types import Ledger
from tangible.metrics.registry import MetricFn

Point = tuple[datetime, Decimal | None]

MIN_SLOPE_POINTS = 4
MIN_BREAK_POINTS = 8


def weekly_series(fn: MetricFn, ledger: Ledger, as_of: datetime, weeks: int) -> list[Point]:
    """`fn(ledger, t)` at t = as_of - 7*i days for i = weeks-1 ... 0 (oldest first, last = as_of).

    Each point applies the time rule at its own date: `fn` is called with that date as its
    as_of, so it must itself go through `ledger.known(t)`.
    """
    times = (as_of - timedelta(days=7 * i) for i in range(weeks - 1, -1, -1))
    return [(t, fn(ledger, t)) for t in times]


def values(points: Iterable[Point]) -> list[Decimal | None]:
    return [v for _, v in points]


def mean(xs: Sequence[Decimal]) -> Decimal | None:
    if not xs:
        return None
    return sum(xs, Decimal(0)) / len(xs)


def stdev(xs: Sequence[Decimal]) -> Decimal | None:
    """Population standard deviation (None for an empty sequence)."""
    m = mean(xs)
    if m is None:
        return None
    return (sum(((x - m) ** 2 for x in xs), Decimal(0)) / len(xs)).sqrt()


def slope(points: Sequence[Decimal | None]) -> Decimal | None:
    """Least-squares slope per step (per week for a weekly series).

    None points are skipped but keep their x position. None if fewer than 4 usable points.
    """
    pts = [(Decimal(i), v) for i, v in enumerate(points) if v is not None]
    if len(pts) < MIN_SLOPE_POINTS:
        return None
    n = Decimal(len(pts))
    sx = sum((x for x, _ in pts), Decimal(0))
    sy = sum((y for _, y in pts), Decimal(0))
    sxx = sum((x * x for x, _ in pts), Decimal(0))
    sxy = sum((x * y for x, y in pts), Decimal(0))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def normalised_slope(points: Sequence[Decimal | None]) -> Decimal | None:
    """`slope` divided by the mean of the usable points: a fraction per step (0.02 = 2%/week).

    None when the slope is None or the mean is 0.
    """
    s = slope(points)
    m = mean([v for v in points if v is not None])
    if s is None or m is None or m == 0:
        return None
    return s / abs(m)


def change(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """Relative change (current - prior) / |prior|. None if either is None or prior is 0."""
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def is_break(
    points: Sequence[Decimal | None], latest: Decimal | None, sigmas: Decimal = Decimal(2)
) -> bool | None:
    """True if `latest` lies outside mean +/- sigmas * stdev of `points`.

    `points` is the baseline (the borrower's own normal range) and should exclude `latest`.
    None if latest is None, fewer than 8 usable points, or the stdev is 0.
    """
    xs = [v for v in points if v is not None]
    if latest is None or len(xs) < MIN_BREAK_POINTS:
        return None
    m, sd = mean(xs), stdev(xs)
    if m is None or sd is None or sd == 0:
        return None
    return abs(latest - m) > sigmas * sd


def window_sum[R](
    rows: Iterable[R],
    when: Callable[[R], datetime],
    value: Callable[[R], Decimal],
    end: datetime,
    days: int,
    back: int = 0,
) -> Decimal:
    """Sum `value` over rows with when in (end - back - days, end - back].

    `back=0` is "the last `days` days"; `back=days` is "the `days` before that".
    Rows must already be time-rule filtered (`ledger.known(...)`).
    """
    hi = end - timedelta(days=back)
    lo = hi - timedelta(days=days)
    return sum((value(r) for r in rows if lo < when(r) <= hi), Decimal(0))
