"""Systemic exposure metrics (set ms-4): pure functions of (ledger + macro signals) as of a date.

The event comes from global macro signals (v1: tariff actions) known at as_of, by publication date.
The borrower's exposure comes from its stated import mix (application v3) and its own cushion
(gross margin, stock already bought, cash). Nothing here forecasts: only enacted or announced
actions count, and the stress assumes the borrower absorbs the whole duty (no pass-through).
None means "not evaluated" (for example, no import mix stated); it never means "no exposure".
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from tangible.ledger.types import AccountBalance, ImportLine, Ledger
from tangible.metrics.health import (
    _ZERO,
    _bounds,
    _net_revenue_known,
    days_of_inventory,
    gross_margin_est,
)
from tangible.metrics.registry import metric

# An action counts when it takes effect in this window around as_of: earlier ones are already in
# the trailing 90-day margin; later ones are too far out to size a loan on.
ALREADY_IN_MARGIN = timedelta(days=90)
LOOKAHEAD = timedelta(days=180)
BUFFER_CAP_DAYS = Decimal(180)
RUNWAY_HORIZON_MONTHS = 24  # stressed_runway is capped here ("lasts the horizon")


def actions_in_window(k: Ledger, as_of: datetime) -> list[dict[str, object]]:
    """Tariff actions in a known ledger that count at as_of (the window above), payload + _ref."""
    out = []
    for m in k.macro_signals:
        if m.kind != "tariff_action":
            continue
        p = json.loads(m.payload_json)
        eff = m.occurred_at
        if eff is None or not (as_of - ALREADY_IN_MARGIN < eff <= as_of + LOOKAHEAD):
            continue
        out.append(p | {"_ref": m.source_ref})
    return out


def added_duty(k: Ledger, as_of: datetime, line: ImportLine) -> tuple[Decimal, list[str]]:
    """Sum of additional duty rates from in-window actions covering this line (they stack), and
    the actions' references."""
    rate, refs = _ZERO, []
    for a in actions_in_window(k, as_of):
        chapters = a.get("hs_chapters") or []
        countries = a.get("countries") or []
        if line.hs_chapter in chapters and line.country in countries:  # type: ignore[operator]
            rate += Decimal(str(a["additional_duty"]))
            refs.append(str(a["_ref"]))
    return rate, refs


def _mix(k: Ledger) -> tuple[ImportLine, ...] | None:
    app = k.application
    return app.import_mix if app is not None else None


@metric("import_share", 1)
def import_share(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Stated share of cost of goods that is imported (sum of the import mix). None when the
    application states no import mix; 0 when it states there are no imports."""
    mix = _mix(ledger.known(as_of))
    if mix is None:
        return None
    return sum((x.share_of_cogs for x in mix), _ZERO)


@metric("tariff_rate_change", 1)
def tariff_rate_change(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Import-weighted additional duty from tariff actions known at as_of and taking effect in
    (as_of - 90d, as_of + 180d]: sum(share_i * added_i) / sum(share_i). 0 with no imports."""
    k = ledger.known(as_of)
    mix = _mix(k)
    if mix is None:
        return None
    total = sum((x.share_of_cogs for x in mix), _ZERO)
    if total == 0:
        return _ZERO
    return sum((x.share_of_cogs * added_duty(k, as_of, x)[0] for x in mix), _ZERO) / total


@metric("tariff_margin_shock", 1)
def tariff_margin_shock(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Share of revenue the extra duty takes if none is passed on:
    import_share * tariff_rate_change * (1 - gross_margin_est). (COGS is (1 - margin) of revenue,
    the duty is charged on the imported part of it.)"""
    share = import_share(ledger, as_of)
    rate = tariff_rate_change(ledger, as_of)
    gm = gross_margin_est(ledger, as_of)
    if share is None or rate is None or gm is None:
        return None
    return share * rate * (1 - gm)


@metric("stock_buffer_months", 1)
def stock_buffer_months(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Months of sales already bought at the old price: min(days_of_inventory, 180) / 30."""
    doi = days_of_inventory(ledger, as_of)
    if doi is None:
        return None
    return min(doi, BUFFER_CAP_DAYS) / 30


@metric("stressed_margin", 1)
def stressed_margin(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """gross_margin_est - tariff_margin_shock."""
    gm = gross_margin_est(ledger, as_of)
    shock = tariff_margin_shock(ledger, as_of)
    if gm is None or shock is None:
        return None
    return gm - shock


def _monthly_flow_and_revenue(k: Ledger, as_of: datetime) -> tuple[Decimal, Decimal] | None:
    start, end = _bounds(as_of)
    rows = [t for t in k.bank_txns if start < t.posted_on <= end]
    if not rows:
        return None
    return sum((t.signed for t in rows), _ZERO) / 3, _net_revenue_known(k, as_of) / 3


@metric("stressed_flow_margin", 1)
def stressed_flow_margin(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Monthly net bank flow once the duty is paid, as a share of monthly net revenue:
    (flow - shock x revenue) / revenue over the trailing 90 days. Negative means the business
    burns cash under the tariff: the cash a loan is repaid from is gone. Ignores the stock buffer
    (it is the steady state after it). None when the shock or the bank flow cannot be evaluated."""
    k = ledger.known(as_of)
    shock = tariff_margin_shock(ledger, as_of)
    fr = _monthly_flow_and_revenue(k, as_of)
    if shock is None or fr is None or fr[1] <= 0:
        return None
    flow, revenue = fr
    return (flow - shock * revenue) / revenue


@metric("stressed_runway", 1)
def stressed_runway(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Months until cash runs out under the tariff shock, capped at 24.

    Start from the latest total balance and the trailing 90-day monthly net bank flow. Each month
    after the stock buffer, the flow falls by shock x monthly net revenue. Fractional final month
    by linear interpolation. 24 means the cash lasts the whole horizon. None when the shock or the
    cash inputs cannot be evaluated.
    """
    k = ledger.known(as_of)
    shock = tariff_margin_shock(ledger, as_of)
    if shock is None:
        return None
    start, end = _bounds(as_of)
    latest: dict[str, AccountBalance] = {}
    for b in k.account_balances:
        if b.as_of <= end and (
            b.account_ref not in latest or b.as_of >= latest[b.account_ref].as_of
        ):
            latest[b.account_ref] = b
    rows = [t for t in k.bank_txns if start < t.posted_on <= end]
    if not latest or not rows:
        return None
    cash = sum((b.balance for b in latest.values()), _ZERO)
    flow = sum((t.signed for t in rows), _ZERO) / 3
    hit = shock * _net_revenue_known(k, as_of) / 3
    buffer = stock_buffer_months(ledger, as_of) or _ZERO
    if cash <= 0:
        return _ZERO
    for month in range(1, RUNWAY_HORIZON_MONTHS + 1):
        # The share of this month past the stock buffer pays the new duty.
        exposed = min(max(Decimal(month) - buffer, _ZERO), Decimal(1))
        delta = flow - hit * exposed
        if cash + delta <= 0:
            return Decimal(month - 1) + (cash / -delta)
        cash += delta
    return Decimal(RUNWAY_HORIZON_MONTHS)


def covering_actions(ledger: Ledger, as_of: datetime) -> tuple[Decimal, list[str]]:
    """Share of COGS covered by an in-window action, and the actions (for checks and display)."""
    k = ledger.known(as_of)
    mix = _mix(k) or ()
    covered, refs = _ZERO, []
    for x in mix:
        rate, r = added_duty(k, as_of, x)
        if rate > 0:
            covered += x.share_of_cogs
            refs += [ref for ref in r if ref not in refs]
    return covered, refs
