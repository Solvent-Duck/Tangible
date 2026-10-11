from decimal import Decimal as D

import pytest

from tangible.numfmt import num, pct, usd


@pytest.mark.parametrize(
    ("x", "out"),
    [(D("1234.5"), "$1,234.50"), (D("0.005"), "$0.01"), (D("-12"), "-$12.00"),
     (D("-0.001"), "$0.00"), (D("98636.715"), "$98,636.72"), (D(0), "$0.00")],
)  # fmt: skip
def test_usd_is_dollars_to_the_cent(x: D, out: str) -> None:
    assert usd(x) == out


@pytest.mark.parametrize(
    ("x", "out"),
    [(D("0.41237"), "0.41"), (D("0.005"), "0.01"), (D("0.3"), "0.30"), (D("183"), "183"),
     (D("1.0"), "1"), (D("-0.0018"), "0.00"), (D("-1.505"), "-1.51"), (D("12.345"), "12.35")],
)  # fmt: skip
def test_num_rounds_half_up_to_two_decimals_and_keeps_whole_numbers(x: D, out: str) -> None:
    assert num(x) == out


@pytest.mark.parametrize(
    ("x", "out"),
    [(D("0.873149"), "87.31%"), (D("0.1"), "10%"), (D("0.01"), "1%"), (D("0.00001"), "<0.01%"),
     (D(0), "0%"), (D("-0.165"), "-16.50%"), (D("0.00005"), "0.01%")],
)  # fmt: skip
def test_pct_writes_a_share_as_a_percent_to_two_decimals(x: D, out: str) -> None:
    assert pct(x) == out


@pytest.mark.parametrize(
    ("value", "key", "out"),
    [("250543.6912", "net_revenue", "$250,543.69"),
     ("86661.05", "stock_value_at_cost", "$86,661.05"),
     ("0.0219", "refund_rate", "2.19%"), ("0.1067", "refund_rate_change_30v30", "+10.67%"),
     ("-0.0304", "revenue_growth_30v30", "−3.04%"),
     ("64.6812", "days_of_inventory", "64.68 days"), ("1", "payout_lag", "1 day"),
     ("10.4", "cash_runway", "10.40 mo"), ("2.1634", "stock_buffer_months", "2.16"),
     ("739", "history_days", "739 days"), (None, "refund_rate", None)],
)  # fmt: skip
def test_metric_text_writes_dollars_and_two_decimals(value: str | None, key: str, out: str) -> None:
    from tangible.workbench.display import metric_text

    assert metric_text(value, key) == out
