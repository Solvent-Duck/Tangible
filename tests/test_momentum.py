import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import tangible.metrics  # noqa: F401
from tangible.ledger.types import BankTxn, Ledger, Order, Refund
from tangible.metrics import REGISTRY, compute
from tangible.metrics.momentum import register_momentum_for
from tangible.metrics.series import (
    change,
    is_break,
    mean,
    normalised_slope,
    slope,
    stdev,
    values,
    weekly_series,
    window_sum,
)

AS_OF = datetime(2026, 3, 1, tzinfo=UTC)
D = Decimal
MOMENTUM_KEYS = [
    "revenue_growth_30v30",
    "revenue_slope_13w",
    "revenue_break_26w",
    "refund_rate_change_30v30",
    "ad_spend_ratio_change_30v30",
    "revenue_yoy_90d",
]


def _order(ref: str, gross: str, days_ago: int, seen_days_ago: int | None = None) -> Order:
    seen = days_ago if seen_days_ago is None else seen_days_ago
    return Order(
        source_ref=ref,
        gross=D(gross),
        discounts=D(0),
        occurred_at=AS_OF - timedelta(days=days_ago),
        observed_at=AS_OF - timedelta(days=seen),
    )


# ---------------------------------------------------------------- pure helpers


def test_slope_known_answer():
    assert slope([D(1), D(3), D(5), D(7)]) == D(2)
    assert slope([D(5), D(5), D(5), D(5)]) == D(0)


def test_slope_skips_none_keeps_x_position():
    assert slope([D(0), None, D(4), None, D(8), D(10)]) == D(2)


def test_slope_none_when_too_few_points():
    assert slope([D(1), D(2), D(3)]) is None
    assert slope([D(1), None, D(3), None, D(5)]) is None
    assert slope([]) is None


def test_normalised_slope():
    assert normalised_slope([D(10), D(20), D(30), D(40)]) == D(10) / D(25)
    assert normalised_slope([D(-1), D(1), D(-1), D(1)]) is None  # mean 0


def test_change():
    assert change(D(110), D(100)) == D("0.1")
    assert change(D(50), D(100)) == D("-0.5")
    assert change(D(1), None) is None
    assert change(None, D(1)) is None
    assert change(D(1), D(0)) is None


def test_mean_stdev():
    xs = [D(2), D(4), D(4), D(4), D(5), D(5), D(7), D(9)]
    assert mean(xs) == D(5)
    assert stdev(xs) == D(2)
    assert mean([]) is None and stdev([]) is None


def test_is_break():
    base = [D(v) for v in (8, 12) * 4]  # mean 10, sd 2
    assert is_break(base, D(15)) is True  # > 10 + 4
    assert is_break(base, D(5)) is True
    assert is_break(base, D(13)) is False
    assert is_break(base, None) is None
    assert is_break(base[:7], D(100)) is None  # < 8 usable points
    assert is_break([None, *base[:7]], D(100)) is None  # None points do not count
    assert is_break([D(5)] * 8, D(6)) is None  # sigma 0


def test_window_sum():
    orders = [_order("a", "10", 5), _order("b", "20", 35), _order("c", "40", 65)]

    def total(back: int) -> Decimal:
        return window_sum(orders, lambda o: o.occurred_at, lambda o: o.gross, AS_OF, 30, back)

    assert (total(0), total(30), total(60)) == (D(10), D(20), D(40))


# ---------------------------------------------------------------- metrics on hand-built ledgers


def test_revenue_growth_known_answer():
    ledger = Ledger(
        orders=(
            _order("n1", "100", 5),
            _order("n2", "50", 29),
            _order("p1", "100", 31),
            _order("p2", "20", 59),
            _order("old", "999", 61),
        ),
        refunds=(
            Refund("r1", "n1", D(30), AS_OF - timedelta(days=3), AS_OF - timedelta(days=3)),
            Refund("r2", "p1", D(10), AS_OF - timedelta(days=40), AS_OF - timedelta(days=40)),
        ),
    )
    # now: 150 - 30 = 120, prior: 120 - 10 = 110
    assert compute("revenue_growth_30v30", 1, ledger, AS_OF) == D(10) / D(110)


def test_refund_rate_and_ad_ratio_change():
    def ad(ref: str, amount: str, days_ago: int, who: str = "GOOGLE ADS UK") -> BankTxn:
        posted = (AS_OF - timedelta(days=days_ago)).date()
        return BankTxn(
            ref, "acc", D(amount), "out", who, None, posted, AS_OF - timedelta(days=days_ago)
        )

    ledger = Ledger(
        orders=(_order("n", "1000", 5), _order("p", "1000", 40)),
        refunds=(
            Refund("r1", "n", D(100), AS_OF - timedelta(days=2), AS_OF - timedelta(days=2)),
            Refund("r2", "p", D(50), AS_OF - timedelta(days=45), AS_OF - timedelta(days=45)),
        ),
        bank_txns=(
            ad("a1", "200", 3),
            ad("a2", "100", 10, "facebook ireland"),
            ad("a3", "100", 50),
            ad("rent", "999", 4, "LANDLORD"),
        ),
    )
    # refund rate 0.10 vs 0.05
    assert compute("refund_rate_change_30v30", 1, ledger, AS_OF) == D(1)
    # now (300 / 900) vs prior (100 / 950)
    expected = (D(300) / D(900) - D(100) / D(950)) / (D(100) / D(950))
    assert compute("ad_spend_ratio_change_30v30", 1, ledger, AS_OF) == expected


def test_none_cases_on_empty_ledger():
    for key in MOMENTUM_KEYS:
        assert compute(key, 1, Ledger(), AS_OF) is None, key


def test_yoy_needs_history():
    short = Ledger(orders=(_order("a", "10", 5), _order("b", "10", 400)))
    assert compute("revenue_yoy_90d", 1, short, AS_OF) is None  # only 400 days back
    long = Ledger(orders=(_order("a", "30", 5), _order("b", "10", 370), _order("c", "1", 460)))
    assert compute("revenue_yoy_90d", 1, long, AS_OF) == D(2)


# ---------------------------------------------------------------- time rule


def _all_values(ledger: Ledger, as_of: datetime) -> dict[str, Decimal | None]:
    return {k: compute(k, 1, ledger, as_of) for k in MOMENTUM_KEYS}


def test_no_lookahead(synthetic_ledger):
    ledger = synthetic_ledger(days=200)
    end = max(o.occurred_at for o in ledger.orders)
    as_of = end - timedelta(days=45)
    later = as_of + timedelta(days=1)

    from tangible.metrics.momentum import _revenue_30d

    before_series = weekly_series(_revenue_30d, ledger, as_of, 26)
    before = _all_values(ledger, as_of)

    past = as_of - timedelta(days=10)
    extra = replace(
        ledger,
        orders=(
            *ledger.orders,
            Order("late-o", D("100000"), D(0), past, later),
            # revision of a real order, observed after as_of
            replace(ledger.orders[-1], gross=D("999999"), observed_at=later),
        ),
        refunds=(*ledger.refunds, Refund("late-r", "late-o", D("50000"), past, later)),
        bank_txns=(
            *ledger.bank_txns,
            BankTxn("late-ad", "acc", D("90000"), "out", "GOOGLE ADS", None, past.date(), later),
        ),
    )
    assert weekly_series(_revenue_30d, extra, as_of, 26) == before_series
    assert _all_values(extra, as_of) == before
    # control: the extra rows do matter once they are known
    assert compute("revenue_growth_30v30", 1, extra, later + timedelta(days=1)) != compute(
        "revenue_growth_30v30", 1, ledger, later + timedelta(days=1)
    )


# ---------------------------------------------------------------- synthetic ledger


def test_synthetic_values_and_runtime(synthetic_ledger):
    ledger = synthetic_ledger(days=400)
    as_of = max(o.occurred_at for o in ledger.orders)
    out: dict[str, Decimal | None] = {}
    for key in MOMENTUM_KEYS:
        t0 = time.perf_counter()
        out[key] = compute(key, 1, ledger, as_of)
        print(f"{key} = {out[key]}  ({time.perf_counter() - t0:.2f}s)")
    assert out["revenue_growth_30v30"] is not None
    assert out["revenue_slope_13w"] is not None
    assert out["revenue_break_26w"] in (D(0), D(1))
    # these two are None only if the prior window has no refunds / no ad spend
    assert out["refund_rate_change_30v30"] is not None
    assert out["ad_spend_ratio_change_30v30"] is not None
    # 400 days < 455 days needed for a full year-ago 90d window
    assert out["revenue_yoy_90d"] is None

    t0 = time.perf_counter()
    weekly_series(REGISTRY[("revenue_growth_30v30", 1)].fn, ledger, as_of, 26)
    assert time.perf_counter() - t0 < 5


def test_synthetic_yoy_with_enough_history(synthetic_ledger):
    ledger = synthetic_ledger(days=520)
    as_of = max(o.occurred_at for o in ledger.orders)
    v = compute("revenue_yoy_90d", 1, ledger, as_of)
    print(f"revenue_yoy_90d (520d) = {v}")
    assert v is not None


# ---------------------------------------------------------------- factory


def test_register_momentum_for():
    def lag(ledger: Ledger, as_of: datetime) -> Decimal | None:
        known = ledger.known(as_of)
        return D(len(known.orders))

    from tangible.metrics import metric

    metric("tmp_lag", 1)(lag)
    try:
        ck, sk = register_momentum_for("tmp_lag", 1)
        assert (ck, sk) == ("tmp_lag_change_30v30", "tmp_lag_slope_13w")
        orders = tuple(_order(f"o{i}", "1", 7 * i) for i in range(1, 12))
        ledger = Ledger(orders=orders)
        # observed when they occurred: 11 known at AS_OF, 7 known 30 days earlier (>= 37d ago)
        assert compute(ck, 1, ledger, AS_OF) == D(4) / D(7)
        assert compute(sk, 1, ledger, AS_OF) is not None
        with pytest.raises(KeyError):
            register_momentum_for("does_not_exist", 1)
    finally:
        for k in ("tmp_lag", "tmp_lag_change_30v30", "tmp_lag_slope_13w"):
            REGISTRY.pop((k, 1), None)


def test_values_helper():
    assert values([(AS_OF, D(1)), (AS_OF, None)]) == [D(1), None]
