"""Systemic exposure checks (set cs-6): tariff exposure as ordinary findings.

Pure functions of (ledger + macro signals, as_of). Only enacted or announced actions known at
as_of count (by publication date). Thresholds are module constants; a change is a new version.
"""

from datetime import datetime
from decimal import Decimal
from typing import Final

from tangible.checks.registry import FindingDraft, check
from tangible.ledger.types import Ledger
from tangible.metrics.systemic import covering_actions, stressed_margin

PILLAR: Final = "health"

# tariff_exposure v1: an action covers at least this share of COGS, and the margin left after the
# extra duty (none passed on) is below STRESSED_MARGIN_MAX.
COVERED_SHARE_MIN: Final = Decimal("0.25")
STRESSED_MARGIN_MAX: Final = Decimal("0.10")


def _q2(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01')):f}"


@check("tariff_exposure", 1)
def tariff_exposure(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    covered, refs = covering_actions(ledger, as_of)
    margin = stressed_margin(ledger, as_of)
    if covered < COVERED_SHARE_MIN or margin is None or margin >= STRESSED_MARGIN_MAX:
        return []
    return [
        FindingDraft(
            "tariff_exposure",
            1,
            "review",
            PILLAR,
            f"Tariff action(s) {', '.join(refs)} cover {_q2(covered * 100)}% of stated cost of "
            f"goods; with none of the duty passed on, gross margin falls to {_q2(margin * 100)}%.",
            facts={
                "covered_share_of_cogs": _q2(covered),
                "stressed_margin": _q2(margin),
                "actions": ",".join(refs),
                "covered_min": str(COVERED_SHARE_MIN),
                "stressed_margin_max": str(STRESSED_MARGIN_MAX),
            },
        )  # fmt: skip
    ]


@check("import_share_unstated", 1)
def import_share_unstated(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """The borrower holds physical stock, but the application states no import mix (info)."""
    k = ledger.known(as_of)
    app = k.application
    if not k.inventory_snapshots or app is None or app.import_mix is not None:
        return []
    return [
        FindingDraft(
            "import_share_unstated",
            1,
            "info",
            PILLAR,
            "The borrower holds physical stock but the application states no import mix, so "
            "tariff exposure could not be evaluated.",
            facts={"inventory_snapshots": str(len(k.inventory_snapshots))},
        )  # fmt: skip
    ]
