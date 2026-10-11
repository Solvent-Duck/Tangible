"""Intangibles checks (set cs-5): negative public signals, as ordinary findings.

Pure functions of (ledger + public signals, as_of). Each starts from `ledger.known(as_of)` and
uses only signals whose entity match counts. No signal, no finding: absence of a listing is not
evidence of anything. Thresholds are module constants; a change is a new check version.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Final

from tangible import numfmt
from tangible.checks.registry import FindingDraft, check
from tangible.intangibles.matching import address_similarity, phone_digits
from tangible.ledger.types import Ledger
from tangible.metrics.intangibles import listings, rating_inputs

PILLAR: Final = "integrity"

# review_volume_vs_orders v1: reviews are a small fraction of orders for a real merchant (a few
# percent is typical). Fire when matched listings carry at least this many reviews and more than
# REVIEWS_PER_ORDER_MAX per order known in the ledger.
REVIEWS_MIN: Final = Decimal(50)
REVIEWS_PER_ORDER_MAX: Final = Decimal("0.25")

# listing_identity_mismatch v1: an address below this similarity (or another street number) or a
# different phone contradicts the application.
ADDRESS_MIN_SIMILARITY: Final = Decimal("0.5")

CLOSED_STATUSES: Final = frozenset({"CLOSED_PERMANENTLY"})


def _q4(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.0001')):f}"


@check("listing_closed", 1)
def listing_closed(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """A matched Google or Yelp listing says the business is permanently closed (hold)."""
    out = []
    for s in listings(ledger.known(as_of)):
        status = s.payload.get("business_status")
        if status in CLOSED_STATUSES:
            out.append(
                FindingDraft(
                    "listing_closed",
                    1,
                    "hold",
                    PILLAR,
                    f"The matched {s.source} listing {s.source_ref} says the business is "
                    "permanently closed.",
                    facts={
                        "source": s.source,
                        "source_ref": s.source_ref,
                        "business_status": str(status),
                        "observed_at": s.observed_at.isoformat(),
                    },
                )  # fmt: skip
            )
    return out


@check("review_volume_vs_orders", 1)
def review_volume_vs_orders(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Review count on matched listings is implausibly high for the orders in the ledger.

    Reviews >= 50 and reviews / orders known up to as_of > 0.25 (review). A common sign of bought
    reviews. Nothing fires without orders (that is the completeness checks' job).
    """
    return _review_volume_vs_orders(ledger, as_of, 1, _q4, str)


@check("review_volume_vs_orders", 2)
def review_volume_vs_orders_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check with the ratio to two decimals (check set cs-7)."""
    return _review_volume_vs_orders(ledger, as_of, 2, numfmt.num, numfmt.num)


def _review_volume_vs_orders(
    ledger: Ledger,
    as_of: datetime,
    version: int,
    ratio_text: Callable[[Decimal], str],
    limit_text: Callable[[Decimal], str],
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    pairs = rating_inputs(k)
    reviews = sum((n for _, n in pairs), Decimal(0))
    orders = sum(1 for o in k.orders if o.occurred_at <= as_of)
    if not pairs or orders == 0 or reviews < REVIEWS_MIN:
        return []
    ratio = reviews / Decimal(orders)
    if ratio <= REVIEWS_PER_ORDER_MAX:
        return []
    return [
        FindingDraft(
            "review_volume_vs_orders",
            version,
            "review",
            PILLAR,
            f"Matched listings show {reviews:f} reviews against {orders} orders in the ledger "
            f"({ratio_text(ratio)} reviews per order, above {limit_text(REVIEWS_PER_ORDER_MAX)}).",
            facts={
                "reviews": f"{reviews:f}",
                "orders": str(orders),
                "ratio": _q4(ratio),
                "threshold": str(REVIEWS_PER_ORDER_MAX),
            },
        )  # fmt: skip
    ]


@check("listing_identity_mismatch", 1)
def listing_identity_mismatch(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """A matched listing's address or phone contradicts the application (review).

    Compared only where both sides have the field. A listing can count as matched with a mismatch
    when an analyst confirmed it (e.g. the business moved); the mismatch is still worth a look.
    """
    k = ledger.known(as_of)
    app = k.application
    if app is None:
        return []
    out = []
    for s in listings(k):
        p = s.payload
        facts: dict[str, str] = {}
        addr = p.get("address")
        if isinstance(addr, str) and addr.strip() and app.address:
            sim = address_similarity(app.address, addr, app.state)
            if sim < ADDRESS_MIN_SIMILARITY:
                facts["address_similarity"] = _q4(sim)
        phone = p.get("phone")
        if isinstance(phone, str) and app.phone and phone_digits(phone) and phone_digits(app.phone):
            if phone_digits(phone) != phone_digits(app.phone):
                facts["phone"] = "differs"
        if facts:
            what = " and ".join(
                n for n, f in (("address", "address_similarity"), ("phone", "phone")) if f in facts
            )
            out.append(
                FindingDraft(
                    "listing_identity_mismatch",
                    1,
                    "review",
                    PILLAR,
                    f"The matched {s.source} listing {s.source_ref} has a different {what} "
                    "from the application.",
                    facts={"source": s.source, "source_ref": s.source_ref, **facts},
                )  # fmt: skip
            )
    return out
