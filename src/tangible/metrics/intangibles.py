"""Intangibles metrics (set ms-3): pure functions of (ledger + public signals) as of a date.

Every metric starts from `ledger.known(as_of)` (public signals included: observed_at decides),
and uses only signals whose entity match counts (`PublicSignal.match_ok`). Listing signals come
from Google Places and Yelp; domain registrations from RDAP. Signal payloads are untrusted data:
these read numbers, dates and flags only, and text only through the loyalty_mentions v1 rule.
None means "not evaluated"; policy never treats it as a pass.
"""

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tangible.intangibles.loyalty import loyalty_share_v1
from tangible.ledger.types import Ledger, PublicSignal
from tangible.metrics.health import _stock_value
from tangible.metrics.registry import metric

LISTING_SOURCES = ("google_places", "yelp")
# review_rating_shrunk v1: prior mean and weight (a few glowing reviews can't beat a long record).
RATING_PRIOR_M = Decimal("3.8")
RATING_PRIOR_C = Decimal(25)
DAYS_PER_YEAR = Decimal("365.25")
_CHANNEL = {"google_places": "google", "yelp": "yelp", "rdap": "website"}
_Q2 = Decimal("0.01")


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        return None
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def _day(v: Any) -> date | None:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def usable(k: Ledger) -> list[PublicSignal]:
    """Known signals whose entity match counts (confirmed, or above threshold and not rejected)."""
    return [s for s in k.public_signals if s.match_ok]


def listings(k: Ledger) -> list[PublicSignal]:
    return [s for s in usable(k) if s.kind == "listing" and s.source in LISTING_SOURCES]


def excerpts(k: Ledger) -> Iterator[dict[str, Any]]:
    for s in listings(k):
        reviews = s.payload.get("reviews")
        if isinstance(reviews, list):
            yield from (r for r in reviews if isinstance(r, dict))


def rating_inputs(k: Ledger) -> list[tuple[Decimal, Decimal]]:
    """(rating, review count) per listing that has both."""
    out = []
    for s in listings(k):
        p = s.payload
        r, n = _num(p.get("rating")), _num(p.get("review_count"))
        if r is not None and n is not None and n >= 0:
            out.append((r, n))
    return out


@metric("review_rating_shrunk", 1)
def review_rating_shrunk(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """(C*m + sum r_i*n_i) / (C + sum n_i) over matched Google + Yelp listings, m = 3.8, C = 25.

    None when no matched listing has a rating and a review count.
    """
    pairs = rating_inputs(ledger.known(as_of))
    if not pairs:
        return None
    num = RATING_PRIOR_C * RATING_PRIOR_M + sum((r * n for r, n in pairs), Decimal(0))
    return num / (RATING_PRIOR_C + sum((n for _, n in pairs), Decimal(0)))


@metric("review_count", 1)
def review_count(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Sum of review counts over matched Google + Yelp listings. None when there are none."""
    pairs = rating_inputs(ledger.known(as_of))
    if not pairs:
        return None
    return sum((n for _, n in pairs), Decimal(0))


@metric("owner_response_rate", 1)
def owner_response_rate(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Review excerpts with an owner reply / review excerpts seen, over matched listings.

    None when no excerpt was seen.
    """
    seen = list(excerpts(ledger.known(as_of)))
    if not seen:
        return None
    return Decimal(sum(1 for r in seen if r.get("owner_reply") is True)) / Decimal(len(seen))


@metric("years_trading", 1)
def years_trading(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """(as_of - earliest of domain registration and first listing review) in years, 2 places.

    Dates come from matched signals and must be on or before the as_of date (state formation dates
    are not sourced in v1). None when no date is known.
    """
    k = ledger.known(as_of)
    end = as_of.date()
    days: list[date] = []
    for s in usable(k):
        p = s.payload
        for field in ("registered_on", "first_review_on"):
            d = _day(p.get(field))
            if d is not None and d <= end:
                days.append(d)
    if not days:
        return None
    return (Decimal((end - min(days)).days) / DAYS_PER_YEAR).quantize(_Q2)


@metric("presence_channels", 1)
def presence_channels(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Distinct confirmed channels: Google listing, Yelp listing, website (a matched RDAP record).

    None when no public signal is known at all (not looked up); 0 when none of them is matched.
    """
    k = ledger.known(as_of)
    if not k.public_signals:
        return None
    return Decimal(len({_CHANNEL[s.source] for s in usable(k) if s.source in _CHANNEL}))


@metric("loyalty_mention_share", 1)
def loyalty_mention_share(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Review excerpts matching the loyalty_mentions v1 keyword rule / excerpts seen.

    Over matched listings. None when no excerpt was seen.
    """
    texts = [
        r["quoted_text"]
        for r in excerpts(ledger.known(as_of))
        if isinstance(r.get("quoted_text"), str)
    ]
    hits, seen = loyalty_share_v1(texts)
    if seen == 0:
        return None
    return Decimal(hits) / Decimal(seen)


@metric("stock_value_at_cost", 1)
def stock_value_at_cost(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """Units on hand x unit cost over the latest costed snapshot per SKU dated <= as_of.

    The stock valuation already used inside gross_margin_est. None with no costed snapshot.
    """
    return _stock_value(ledger.known(as_of), as_of.date())


@metric("stock_cover", 1)
def stock_cover(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """stock_value_at_cost / the amount requested on the application.

    v1 uses the application's requested amount (a metric cannot see the policy's band limits).
    None when either is missing or the requested amount is not positive.
    """
    k = ledger.known(as_of)
    app = k.application
    value = _stock_value(k, as_of.date())
    if app is None or app.requested_amount is None or app.requested_amount <= 0 or value is None:
        return None
    return value / app.requested_amount


@metric("entity_match_ok", 1)
def entity_match_ok(ledger: Ledger, as_of: datetime) -> Decimal | None:
    """1 when every known public signal comes from a match that counts, else 0.

    A match stops counting when an analyst rejects it. None when no signal is known.
    """
    k = ledger.known(as_of)
    if not k.public_signals:
        return None
    return Decimal(1 if all(s.match_ok for s in k.public_signals) else 0)
