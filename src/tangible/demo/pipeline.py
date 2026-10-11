"""Demo onboarding pipeline: four invited US businesses at different stages, so the lender
dashboard has something to show and one business is ready for diligence.

Everything goes through the onboarding services exactly as the web app drives them (invite,
open, accept, profile, simulated connect, submit), so outbox emails, events and the
`application` row are real. The businesses and their people are fictional.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from tangible.db.session import tenant_session
from tangible.onboarding import services as onboarding
from tangible.workbench.auth import Identity, Principal, lookup_principal
from tangible.workbench.settings import Settings


@dataclass(frozen=True)
class Invitee:
    business: str
    contact: str
    email: str
    loan: Decimal
    items: tuple[str, ...]
    stage: str  # invited | opened | connecting | submitted
    profile: dict[str, Any] | None = None
    connect: tuple[tuple[str, str], ...] = ()  # (item, provider)


PIPELINE: Final[tuple[Invitee, ...]] = (
    Invitee("Cedar & Sage Florist LLC", "Priya Raman", "priya@cedarsage.test",
            Decimal("25000"), ("website", "google_business", "stock_report"), "invited"),
    Invitee("Bluebird Cycle Works Inc", "Tom Okafor", "tom@bluebirdcycles.test",
            Decimal("40000"), ("inventory_system", "stock_report", "website", "reviews"),
            "opened"),
    Invitee("Harborview Optics LLC", "Dana Whitfield", "dana@harborviewoptics.test",
            Decimal("55000"), ("entity", "year_founded", "website", "google_business"),
            "connecting",
            profile={"legal_name": "Harborview Optics LLC", "state": "ME", "year_founded": 2011,
                     "website": "harborviewoptics.test"},
            connect=(("bank", "plaid"),)),
    Invitee("Lakeside Furniture Co", "Marcus Bell", "marcus@lakesidefurniture.test",
            Decimal("90000"),
            ("inventory_system", "stock_report", "entity", "year_founded", "import_mix",
             "website"),
            "submitted",
            profile={"legal_name": "Lakeside Furniture Company LLC", "dba": "Lakeside Furniture",
                     "state": "MI", "year_founded": 2006, "website": "lakesidefurniture.test",
                     "stated_revenue_90d": "310000",
                     "import_mix": [{"hs_chapter": "94", "country": "CN", "share_of_cogs": "0.55"},
                                    {"hs_chapter": "44", "country": "CA",
                                     "share_of_cogs": "0.15"}]},
            connect=(("bank", "plaid"), ("accounting", "quickbooks"), ("sales", "shopify"),
                     ("inventory_system", "shopify"))),
)  # fmt: skip


def seed_pipeline(
    settings: Settings, admin: Principal, log: Callable[[str], None] = print
) -> list[str]:
    """Create the invites and walk each to its stage. Returns the invite ids."""
    url, tenant = settings.database_url, admin.tenant_id
    ids: list[str] = []
    for inv in PIPELINE:
        with tenant_session(url, tenant) as conn:
            sent = onboarding.create_invite(conn, settings, admin, {
                "email": inv.email, "business_name": inv.business, "contact_name": inv.contact,
                "loan_amount": str(inv.loan), "items": list(inv.items),
                "message": "Thanks for considering us. This replaces the usual statement chase.",
            })  # fmt: skip
        ids.append(sent["id"])
        token = sent["link"].rsplit("/invite/", 1)[1]
        if inv.stage != "invited":
            onboarding.preview_invite(url, token)
        if inv.stage in ("connecting", "submitted"):
            subject = f"demo:{inv.email}"
            onboarding.accept_invite(url, token, Identity(subject, inv.email))
            owner = lookup_principal(url, tenant, subject)
            with tenant_session(url, tenant) as conn:
                if inv.profile:
                    onboarding.save_profile(conn, owner, inv.profile)
                for item, provider in inv.connect:
                    onboarding.connect_source(conn, owner, item, provider)
                if inv.stage == "submitted":
                    onboarding.submit(conn, owner)
        log(f"  invited {inv.business}: {inv.stage}")
    return ids
