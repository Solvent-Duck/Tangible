"""What a lender can ask a borrower for: the request catalog behind the invite composer.

The catalog is versioned like the metric and check sets. An invite stores the catalog version it
was composed against, so what was asked is always readable later. Changing an item means a new
version; a released version is never edited.

Each item says how the borrower answers it (`kind`) and what it feeds (`feeds`):
- "ledger": the data feeds today's metrics and checks (financials, inventory, repeat customers);
- "intangibles": collected now for the intangibles pillar (docs/intangibles-plan.md), which is
  still being built. The UI says so rather than implying the data is scored today.

No government identifiers (SSN, EIN, passport) are ever requested.
"""

from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

Kind = Literal["connector", "upload", "field", "link"]
Feeds = Literal["ledger", "intangibles", "lender"]

CURRENT_VERSION: Final = "rc-1"
MAX_CUSTOM: Final = 5
MAX_CUSTOM_LABEL: Final = 120


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    blurb: str


@dataclass(frozen=True)
class Item:
    id: str
    group: str
    label: str
    help: str
    kind: Kind
    feeds: Feeds
    locked: bool = False  # always requested; the lender cannot switch it off
    default_on: bool = False
    # For connectors: the providers the borrower can pick from (the `connection.source` written).
    providers: tuple[str, ...] = ()


GROUPS: Final[tuple[Group, ...]] = (
    Group("financials", "Standard financials",
          "Always requested. Bank, books and sales data are the base of every assessment."),
    Group("inventory", "Inventory", "Stock on hand, its age and how fast it turns."),
    Group("presence", "Digital presence", "Where the business shows up online."),
    Group("reputation", "Reputation & loyalty", "What customers say and whether they come back."),
    Group("longevity", "Longevity & entity", "Who the business is and how long it has traded."),
    Group("community", "Community", "Local press, awards and associations."),
)  # fmt: skip

_ITEMS: Final[tuple[Item, ...]] = (
    # standard financials
    Item("bank", "financials", "Business bank accounts",
         "Read-only transactions and balances.", "connector", "ledger", locked=True,
         providers=("plaid",)),
    Item("accounting", "financials", "Accounting system",
         "Invoices, bills and the general ledger.", "connector", "ledger", locked=True,
         providers=("quickbooks", "xero", "freshbooks")),
    Item("sales", "financials", "Sales & payment processors",
         "Orders, refunds and payouts.", "connector", "ledger", locked=True,
         providers=("shopify", "stripe", "square", "amazon", "etsy")),
    Item("bank_statements", "financials", "Bank statements (PDF)",
         "Fallback for any account that cannot be connected.", "upload", "ledger", locked=True),
    # inventory
    Item("inventory_system", "inventory", "Inventory system",
         "Live stock levels and cost.", "connector", "ledger", default_on=True,
         providers=("shopify", "quickbooks", "cin7", "lightspeed")),
    Item("stock_report", "inventory", "Stock report (CSV or spreadsheet)",
         "Item, quantity on hand and unit cost.", "upload", "ledger", default_on=True),
    Item("aged_stock", "inventory", "Aged stock report",
         "Stock by age band, if the system produces one.", "upload", "ledger"),
    # digital presence
    Item("website", "presence", "Website", "The business's own site.", "link", "intangibles",
         default_on=True),
    Item("google_business", "presence", "Google Business profile",
         "The listing customers find on Maps.", "link", "intangibles", default_on=True),
    Item("yelp", "presence", "Yelp listing", "If the business has one.", "link", "intangibles"),
    Item("social", "presence", "Social profiles",
         "Instagram, Facebook, TikTok or LinkedIn.", "link", "intangibles"),
    # reputation & loyalty
    Item("reviews", "reputation", "Review platforms",
         "Links to Trustpilot, Google or marketplace reviews.", "link", "intangibles"),
    Item("customers", "reputation", "Customer & repeat-purchase data",
         "Comes from the sales connection; shows how many customers come back.",
         "connector", "ledger", providers=("shopify", "square", "stripe")),
    # longevity & entity
    Item("entity", "longevity", "Legal name, DBA and state of registration",
         "Who the business is on paper.", "field", "intangibles", default_on=True),
    Item("year_founded", "longevity", "Year founded", "When the business started trading.",
         "field", "intangibles", default_on=True),
    # community
    Item("press", "community", "Press & awards", "Links to coverage, awards or memberships.",
         "link", "intangibles"),
)  # fmt: skip

ITEMS: Final[dict[str, Item]] = {i.id: i for i in _ITEMS}
GROUP_IDS: Final = frozenset(g.id for g in GROUPS)


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CustomItem:
    id: str  # custom-1 .. custom-5
    label: str
    kind: Literal["upload", "field"]


def custom_items(raw: list[dict[str, Any]] | None) -> tuple[CustomItem, ...]:
    """Validate lender-defined requests: at most MAX_CUSTOM, each a short label and upload/field."""
    raw = raw or []
    if len(raw) > MAX_CUSTOM:
        raise CatalogError(f"at most {MAX_CUSTOM} custom requests")
    out: list[CustomItem] = []
    for n, r in enumerate(raw, start=1):
        label = str(r.get("label", "")).strip()
        kind = r.get("kind", "upload")
        if not label or len(label) > MAX_CUSTOM_LABEL:
            raise CatalogError(f"custom request {n} needs a label of 1-{MAX_CUSTOM_LABEL} chars")
        if kind not in ("upload", "field"):
            raise CatalogError(f"custom request {n}: kind must be 'upload' or 'field'")
        out.append(CustomItem(f"custom-{n}", label, kind))
    return tuple(out)


def resolve_requests(selected: list[str], custom: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The `requests` document stored on an invite: the locked items plus the lender's choices,
    in catalog order, and validated custom items. Unknown ids are an error, not ignored."""
    unknown = sorted(set(selected) - ITEMS.keys())
    if unknown:
        raise CatalogError(f"unknown request item(s): {', '.join(unknown)}")
    chosen = set(selected) | {i.id for i in _ITEMS if i.locked}
    return {
        "items": [i.id for i in _ITEMS if i.id in chosen],
        "custom": [asdict(c) for c in custom_items(custom)],
    }


def requested_items(requests: dict[str, Any]) -> list[dict[str, Any]]:
    """The requests document expanded for display: catalog items then custom ones."""
    out: list[dict[str, Any]] = []
    for item_id in requests.get("items", []):
        item = ITEMS.get(item_id)
        if item is not None:
            out.append(item_view(item))
    for c in requests.get("custom", []):
        out.append(
            {"id": c["id"], "group": "custom", "label": c["label"],
             "help": "Requested by the lender.", "kind": c["kind"], "feeds": "lender",
             "locked": False, "providers": []}
        )  # fmt: skip
    return out


def item_view(item: Item) -> dict[str, Any]:
    d = asdict(item)
    d["providers"] = list(item.providers)
    return d


def catalog_view() -> dict[str, Any]:
    return {
        "version": CURRENT_VERSION,
        "groups": [asdict(g) for g in GROUPS],
        "items": [item_view(i) for i in _ITEMS],
        "max_custom": MAX_CUSTOM,
    }
