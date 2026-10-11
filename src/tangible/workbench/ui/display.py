"""Display-only Jinja filters. They format values for reading; they never change what is stored,
and every rounded value keeps its exact form in a `title` attribute."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from markupsafe import Markup, escape

# Metric keys whose values are fractions (shown as percentages) or durations. Anything else is
# shown as a plain number, so a new metric is never mislabelled, only left unformatted.
_PERCENT_SUFFIXES = (
    "_share", "_rate", "_ratio", "_discipline", "_margin_est", "_30v30", "_yoy_90d",
)  # fmt: skip
_DAY_KEYS = {"history_days", "payables_days", "days_of_inventory", "payout_lag"}
_MONTH_KEYS = {"cash_runway"}
_SIGNED_SUFFIXES = ("_30v30", "_yoy_90d", "_slope_13w")

ROLE_LABELS = {
    "analyst": "Analyst",
    "credit_admin": "Credit admin",
    "lender_underwriter": "Lender underwriter",
}


def _decimal(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def when(v: Any, date_only: bool = False) -> Markup:
    """An ISO timestamp as `2026-10-02 23:59 UTC` in a <time> element (exact value on hover)."""
    d = _parse_dt(v)
    if d is None:
        return Markup(escape("" if v is None else v))
    raw = d.isoformat()
    if d.tzinfo is not None:
        d = d.astimezone(UTC)
    text = d.strftime("%Y-%m-%d") if date_only else d.strftime("%Y-%m-%d %H:%M UTC")
    return Markup('<time datetime="{0}" title="{0}">{1}</time>').format(raw, text)


def amount(v: Any) -> str:
    """A money-like decimal with thousands separators and two places; '—' when absent."""
    d = _decimal(v)
    if d is None:
        return "—"
    return f"{d:,.2f}"


def _trim(d: Decimal, places: int) -> str:
    s = f"{d:,.{places}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _signed(s: str, d: Decimal) -> str:
    if d > 0:
        return "+" + s
    if s.startswith("-"):
        return "−" + s[1:]
    return s


def metric_value(v: Any, key: str) -> Markup:
    """A metric value, readable: fractions as percentages, durations with units. The exact
    stored value is kept in the title."""
    d = _decimal(v)
    if d is None:
        return Markup('<span class="na">not available</span>')
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
    return Markup('<span title="{0}">{1}</span>').format(str(v), text)


def short_id(v: Any) -> str:
    return str(v or "")[:8]


def humanize(v: Any) -> str:
    """`raises_risk` -> `Raises risk`; role keys get their proper names."""
    s = str(v or "")
    if s in ROLE_LABELS:
        return ROLE_LABELS[s]
    s = s.replace("_", " ").strip()
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------- a small, safe Markdown subset

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _inline(text: str) -> str:
    # `text` is already escaped; only our own tags are added.
    text = _CODE.sub(r"<code>\1</code>", text)
    return _BOLD.sub(r"<strong>\1</strong>", text)


def markdown(v: Any) -> Markup:
    """Headings, paragraphs, bullet and numbered lists, **bold** and `code`, nothing else.

    The input is untrusted (model output): it is HTML-escaped first, so the only markup in the
    result is the handful of tags added here."""
    lines = str(escape(v or "")).splitlines()
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in lines:
        if not line.strip():
            flush_para()
            close_list()
            continue
        h = _HEADING.match(line)
        b = _BULLET.match(line)
        n = _NUMBERED.match(line)
        if h:
            flush_para()
            close_list()
            level = "h4" if len(h.group(1)) <= 2 else "h5"
            out.append(f"<{level}>{_inline(h.group(2))}</{level}>")
        elif b or n:
            flush_para()
            tag = "ul" if b else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append("<li>" + _inline((b or n).group(1)) + "</li>")  # type: ignore[union-attr]
        else:
            close_list()
            para.append(line.strip())
    flush_para()
    close_list()
    return Markup("\n".join(out))


def pipes(v: Any) -> Markup:
    """A raw pipe-delimited ledger line with the separators dimmed."""
    parts = [str(escape(p)) for p in str(v).split("|")]
    return Markup('<span class="pipe">|</span>'.join(parts))


# Human labels for metric keys (the key stays visible beside it for traceability).
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
    "review_rating_shrunk": "Review rating (shrunk toward 3.8)",
    "review_count": "Reviews on matched listings",
    "owner_response_rate": "Owner replies to reviews",
    "years_trading": "Years trading (public records)",
    "presence_channels": "Confirmed public channels",
    "loyalty_mention_share": "Reviews mentioning return visits",
    "stock_value_at_cost": "Stock value at cost",
    "stock_cover": "Stock cover of requested amount",
    "entity_match_ok": "All public signals matched",
    "import_share": "Imported share of cost of goods (stated)",
    "tariff_rate_change": "New tariff on imports (weighted)",
    "tariff_margin_shock": "Revenue lost to new tariffs (none passed on)",
    "stock_buffer_months": "Months of stock bought at the old price",
    "stressed_margin": "Gross margin after new tariffs",
    "stressed_flow_margin": "Monthly cash flow after new tariffs (share of revenue)",
    "stressed_runway": "Months of cash after new tariffs (max 24)",
}


def metric_label(key: Any) -> str:
    k = str(key or "")
    return METRIC_LABELS.get(k) or humanize(k)


FILTERS = {
    "when": when,
    "amount": amount,
    "metric_value": metric_value,
    "metric_label": metric_label,
    "short_id": short_id,
    "humanize": humanize,
    "markdown": markdown,
    "pipes": pipes,
}
