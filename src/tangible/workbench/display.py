"""Display text for metric values and keys. Display only: it formats values for reading and
never changes what is stored (the exact value always travels beside the text)."""

from decimal import Decimal, InvalidOperation
from typing import Any

# Metric keys whose values are fractions (shown as percentages) or durations. Anything else is
# shown as a plain number, so a new metric is never mislabelled, only left unformatted.
_PERCENT_SUFFIXES = (
    "_share", "_rate", "_ratio", "_discipline", "_margin_est", "_30v30", "_yoy_90d",
)  # fmt: skip
_DAY_KEYS = {"history_days", "payables_days", "days_of_inventory", "payout_lag"}
_MONTH_KEYS = {"cash_runway"}
_SIGNED_SUFFIXES = ("_30v30", "_yoy_90d", "_slope_13w")


def _decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _trim(d: Decimal, places: int) -> str:
    s = f"{d:,.{places}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _signed(s: str, d: Decimal) -> str:
    if d > 0:
        return "+" + s
    if s.startswith("-"):
        return "\u2212" + s[1:]
    return s


def metric_text(v: Any, key: str) -> str | None:
    """`metric_value` as plain text (None when the value is absent), for the JSON API."""
    d = _decimal(v)
    if d is None:
        return None
    signed = key.endswith(_SIGNED_SUFFIXES)
    if key.endswith(_PERCENT_SUFFIXES):
        text = _trim(d * 100, 1) + "%"
    elif key in _DAY_KEYS:
        text = _trim(d, 1) + (" day" if d == 1 else " days")
    elif key in _MONTH_KEYS:
        text = _trim(d, 1) + " mo"
    elif key == "net_revenue":
        text = f"{d:,.2f}"
    else:
        text = _trim(d, 4)
    if signed:
        text = _signed(text, d)
    return text


def humanize(v: Any) -> str:
    """`raises_risk` -> `Raises risk`."""
    s = str(v or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:]


METRIC_LABELS: dict[str, str] = {
    "net_revenue": "Net revenue (last 90 days)",
    "refund_rate": "Refund rate",
    "gross_margin_est": "Gross margin (estimated)",
    "repeat_customer_share": "Repeat-customer share of revenue",
    "days_of_inventory": "Days of inventory",
    "aged_stock_share": "Aged stock share",
    "cash_runway": "Cash runway",
    "payables_discipline": "Payables paid on time",
    "payout_lag": "Payout lag (median)",
    "history_days": "Trading history",
    "payables_days": "Days to pay suppliers",
    "revenue_growth_30v30": "Revenue growth (30 vs prior 30 days)",
    "revenue_slope_13w": "Revenue trend (13 weeks)",
    "revenue_break_26w": "Revenue break vs 26-week range",
    "refund_rate_change_30v30": "Refund rate change (30 vs prior 30 days)",
    "ad_spend_ratio_change_30v30": "Ad spend ratio change (30 vs prior 30 days)",
    "revenue_yoy_90d": "Revenue vs a year earlier (90 days)",
    "payout_lag_change_30v30": "Payout lag change (now vs 30 days ago)",
    "payout_lag_slope_13w": "Payout lag trend (13 weeks)",
    "days_of_inventory_change_30v30": "Days of inventory change (now vs 30 days ago)",
    "days_of_inventory_slope_13w": "Days of inventory trend (13 weeks)",
    "payables_days_change_30v30": "Days to pay suppliers change (now vs 30 days ago)",
    "payables_days_slope_13w": "Days to pay suppliers trend (13 weeks)",
}


def metric_label(key: Any) -> str:
    k = str(key or "")
    return METRIC_LABELS.get(k) or humanize(k)
