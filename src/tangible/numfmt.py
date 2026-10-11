"""Number formatting for text people read: findings, policy reasons, reports.

Display only. Nothing here feeds a computation: checks, metrics and policy compare the exact
values, and facts keep full precision. Released check and policy versions keep their own
formatting; versions that write "$" and two decimals (check set cs-7, policy 0.6.0) use these.

- `usd`: dollars to the cent with thousands separators, "$1,234.56", "-$12.00".
- `num`: two decimals, half up, thousands separated; a whole number stays whole ("183",
  "0.42", "-1.50", "1,234.57").
- `pct`: a fraction as a percent with `num` rules ("87.31%", "10%"); a positive share too small
  to show at two decimals is "<0.01%", never "0%".
"""

from decimal import ROUND_HALF_UP, Decimal

_PLACES = Decimal("0.01")


def _q(x: Decimal) -> Decimal:
    q = x.quantize(_PLACES, rounding=ROUND_HALF_UP)
    return q + 0 if q.is_zero() else q  # drop the sign of a negative zero


def usd(x: Decimal) -> str:
    q = _q(Decimal(x))
    return f"-${-q:,.2f}" if q < 0 else f"${q:,.2f}"


def num(x: Decimal) -> str:
    d = Decimal(x)
    if d.is_zero():
        return "0"
    if d == d.to_integral_value():
        return f"{d.to_integral_value():,f}"
    return f"{_q(d):,f}"


def pct(share: Decimal) -> str:
    p = Decimal(share) * 100
    if 0 < abs(p) < Decimal("0.005"):
        return "<0.01%" if p > 0 else ">-0.01%"
    return f"{num(_q(p))}%"
