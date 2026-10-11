"""entity_match: score a source candidate against the application's stated identity.

Pure and versioned. A new formula, weight or normalisation is a new version (add `score_v2`,
never edit v1). The agent proposes a candidate; this scores it; the agent cannot override it.

v1, for a listing (Google Places / Yelp):
- name: token Jaccard of the normalised names (legal suffixes, '&'/'and', punctuation dropped),
  the better of the application's legal name and DBA;
- address: 0 unless the street numbers agree, else the overlap coefficient of the normalised
  street + city + state tokens (ZIP and country dropped, common abbreviations unified);
- phone: 1 when the last 10 digits agree, else 0;
- website: 1 when the host (minus "www.") agrees, else 0.
Weights 0.4 / 0.3 / 0.2 / 0.1. The score is the weighted mean over the fields present on both
sides; when fewer than two fields can be compared it is capped at 0.5 (a name alone never clears
the threshold). Rounded down to 4 places.

v1, for a domain registration (RDAP): 1 when the domain is the application's website host, else
0. The domain is the identity being looked up.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlsplit

from tangible.ledger.types import Application

MATCHER_VERSION = 1
WEIGHTS_V1: Mapping[str, Decimal] = {
    "name": Decimal("0.4"),
    "address": Decimal("0.3"),
    "phone": Decimal("0.2"),
    "website": Decimal("0.1"),
}
MIN_FIELDS_V1 = 2
SINGLE_FIELD_CAP_V1 = Decimal("0.5")
_Q = Decimal("0.0001")

_NAME_NOISE = frozenset(
    {
        "llc", "inc", "incorporated", "co", "corp", "corporation", "company", "ltd", "limited",
        "the", "and", "pllc", "lp", "llp", "l", "c",
    }
)  # fmt: skip
_ADDRESS_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "highway": "hwy",
    "parkway": "pkwy", "suite": "ste", "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}  # fmt: skip
_COUNTRY = frozenset({"usa", "us", "united", "states"})


@dataclass(frozen=True)
class Identity:
    names: tuple[str, ...]
    address: str | None
    state: str | None
    phone: str | None
    website: str | None


def identity_of(app: Application) -> Identity:
    names = tuple(n for n in (app.legal_name, app.dba) if n)
    return Identity(names, app.address, app.state, app.phone, app.website)


def has_identity(ident: Identity) -> bool:
    return bool(ident.names or ident.address or ident.phone or ident.website)


def _tokens(s: str) -> list[str]:
    s = s.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def name_tokens(s: str) -> frozenset[str]:
    return frozenset(t for t in _tokens(s) if t not in _NAME_NOISE)


def name_similarity(a: str, b: str) -> Decimal:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return Decimal(0)
    return Decimal(len(ta & tb)) / Decimal(len(ta | tb))


def address_tokens(address: str, state: str | None = None) -> tuple[str | None, frozenset[str]]:
    """(street number, tokens). ZIP codes and country words are dropped."""
    raw = _tokens(address)
    number = raw[0] if raw and raw[0].isdigit() else None
    out = set()
    for i, t in enumerate(raw):
        if t in _COUNTRY or (i > 0 and re.fullmatch(r"\d{5}(\d{4})?", t)):
            continue
        out.add(_ADDRESS_ABBREV.get(t, t))
    if state:
        out.add(state.strip().lower())
    return number, frozenset(out)


def address_similarity(a: str, b: str, state_a: str | None = None) -> Decimal:
    na, ta = address_tokens(a, state_a)
    nb, tb = address_tokens(b)
    if na is None or nb is None or na != nb or not ta or not tb:
        return Decimal(0)
    return Decimal(len(ta & tb)) / Decimal(min(len(ta), len(tb)))


def phone_digits(s: str) -> str:
    d = re.sub(r"\D", "", s)
    return d[-10:] if len(d) >= 10 else d


def domain_of(s: str) -> str:
    s = s.strip().lower()
    host = urlsplit(s if "://" in s else f"https://{s}").hostname or ""
    return host.removeprefix("www.").rstrip(".")


def _present(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def score_v1(ident: Identity, kind: str, data: Mapping[str, Any]) -> tuple[Decimal, dict[str, str]]:
    """(score in [0, 1], per-field components as strings for the audit trail)."""
    if kind == "domain_registration":
        domain = _present(data.get("domain"))
        site = domain_of(ident.website) if ident.website else ""
        ok = bool(domain and site and domain_of(domain) == site)
        score = Decimal(1) if ok else Decimal(0)
        return score, {"website": str(score), "rule": "domain is the application's website"}

    sims: dict[str, Decimal] = {}
    name = _present(data.get("name"))
    if name and ident.names:
        sims["name"] = max(name_similarity(n, name) for n in ident.names)
    address = _present(data.get("address"))
    if address and ident.address:
        sims["address"] = address_similarity(ident.address, address, ident.state)
    phone = _present(data.get("phone"))
    if phone and ident.phone and phone_digits(ident.phone) and phone_digits(phone):
        sims["phone"] = Decimal(1 if phone_digits(ident.phone) == phone_digits(phone) else 0)
    website = _present(data.get("website"))
    if website and ident.website and domain_of(website) and domain_of(ident.website):
        sims["website"] = Decimal(1 if domain_of(ident.website) == domain_of(website) else 0)

    components = {k: str(v.quantize(_Q, ROUND_DOWN)) for k, v in sims.items()}
    if not sims:
        return Decimal(0), {**components, "rule": "no comparable field"}
    weight = sum((WEIGHTS_V1[k] for k in sims), Decimal(0))
    score = sum((WEIGHTS_V1[k] * v for k, v in sims.items()), Decimal(0)) / weight
    if len(sims) < MIN_FIELDS_V1:
        score = min(score, SINGLE_FIELD_CAP_V1)
        components["rule"] = f"fewer than {MIN_FIELDS_V1} comparable fields: capped"
    return score.quantize(_Q, ROUND_DOWN), components
