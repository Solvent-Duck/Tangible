"""Public-signal sources for the intangibles tools: place search (Google Places, Yelp), web search
and RDAP. v1 is fixtures only: no live adapter exists and nothing here opens a socket.

The agent names a source and a query (a business name or phrase, or a bare domain for RDAP),
never a URL. Each source's JSON is read by a fixed parser (no model) into plain fields. All of it
is untrusted data: names, addresses and review text can carry instructions or lies.

Vendor JSON shapes below follow the public API docs loosely (Places API v1 `places`, Yelp Fusion
`businesses`, RDAP `events`) plus the review fields we need; confirm field names, terms of use and
storage limits before a live adapter is written (see the plan's open questions).
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

PLACE_SOURCES = ("google_places", "yelp")
MAX_RESULTS = 10
MAX_REVIEWS = 20
MAX_TEXT = 1_000

_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class SignalError(Exception):
    """A lookup failure whose message is safe to show the model."""


@dataclass(frozen=True)
class SourceResult:
    """One candidate from a source. `data` holds parsed, JSON-safe fields (untrusted)."""

    source: str  # google_places | yelp | search | rdap
    source_ref: str  # place id, business id, result URL, or domain
    kind: str  # listing | search_result | domain_registration
    data: dict[str, Any]
    url: str | None
    fetched_at: datetime
    sha256: str  # of the raw response body


class SignalProvider(Protocol):
    def place_search(self, source: str, query: str) -> list[SourceResult]: ...
    def web_search(self, query: str) -> list[SourceResult]: ...
    def rdap(self, domain: str) -> SourceResult | None: ...


def valid_domain(s: str) -> bool:
    return bool(_DOMAIN.match(s))


# ---------------------------------------------------------------- parsers (fixed, no model)


def _s(v: Any, limit: int = 300) -> str | None:
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def _n(v: Any) -> int | float | None:
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


def _review(text: Any, rating: Any, when: Any, reply: Any) -> dict[str, Any] | None:
    t = _s(text, MAX_TEXT)
    if t is None:
        return None
    return {
        "quoted_text": t,
        "rating": _n(rating),
        "published_on": (_s(when) or "")[:10] or None,
        "owner_reply": reply is True,
    }


def parse_google_places(body: bytes) -> list[tuple[str, dict[str, Any], str | None]]:
    """Places API v1-style `{"places": [...]}` -> (place id, listing fields, url)."""
    out = []
    for p in json.loads(body).get("places", [])[:MAX_RESULTS]:
        pid = _s(p.get("id"))
        if pid is None:
            continue
        reviews = [
            r
            for x in p.get("reviews", [])[:MAX_REVIEWS]
            if (r := _review((x.get("text") or {}).get("text"), x.get("rating"),
                             x.get("publishTime"), x.get("ownerReply"))) is not None
        ]  # fmt: skip
        out.append(
            (
                pid,
                {
                    "name": _s((p.get("displayName") or {}).get("text")),
                    "address": _s(p.get("formattedAddress")),
                    "phone": _s(p.get("nationalPhoneNumber")),
                    "website": _s(p.get("websiteUri")),
                    "rating": _n(p.get("rating")),
                    "review_count": _n(p.get("userRatingCount")),
                    "business_status": _s(p.get("businessStatus")),
                    "first_review_on": (_s(p.get("firstReviewTime")) or "")[:10] or None,
                    "reviews": reviews,
                },
                _s(p.get("googleMapsUri")),
            )
        )
    return out


def parse_yelp(body: bytes) -> list[tuple[str, dict[str, Any], str | None]]:
    """Yelp Fusion-style `{"businesses": [...]}` -> (business id, listing fields, url)."""
    out = []
    for b in json.loads(body).get("businesses", [])[:MAX_RESULTS]:
        bid = _s(b.get("id"))
        if bid is None:
            continue
        lines = (b.get("location") or {}).get("display_address") or []
        reviews = [
            r
            for x in b.get("reviews", [])[:MAX_REVIEWS]
            if (r := _review(x.get("text"), x.get("rating"), x.get("time_created"),
                             x.get("owner_reply"))) is not None
        ]  # fmt: skip
        closed = b.get("is_closed")
        out.append(
            (
                bid,
                {
                    "name": _s(b.get("name")),
                    "address": _s(", ".join(x for x in lines if isinstance(x, str))),
                    "phone": _s(b.get("display_phone")),
                    "website": _s(b.get("website")),
                    "rating": _n(b.get("rating")),
                    "review_count": _n(b.get("review_count")),
                    "business_status": "CLOSED_PERMANENTLY" if closed is True else "OPERATIONAL",
                    "first_review_on": (_s(b.get("first_review_on")) or "")[:10] or None,
                    "reviews": reviews,
                },
                _s(b.get("url")),
            )
        )
    return out


def parse_search(body: bytes) -> list[tuple[str, dict[str, Any], str | None]]:
    out: list[tuple[str, dict[str, Any], str | None]] = []
    for r in json.loads(body).get("results", [])[:MAX_RESULTS]:
        url = _s(r.get("url"), 500)
        if url is None:
            continue
        out.append(
            (url, {"title": _s(r.get("title")), "snippet": _s(r.get("snippet"), MAX_TEXT)}, url)
        )
    return out


def parse_rdap(body: bytes) -> tuple[str, dict[str, Any]]:
    d = json.loads(body)
    domain = (_s(d.get("ldhName")) or "").lower()
    registered = None
    for e in d.get("events", []):
        if e.get("eventAction") == "registration":
            registered = (_s(e.get("eventDate")) or "")[:10] or None
    registrar = None
    for ent in d.get("entities", []):
        if "registrar" in (ent.get("roles") or []):
            registrar = _s(ent.get("name"))
    return domain, {"domain": domain, "registered_on": registered, "registrar": registrar}


_PARSERS = {"google_places": parse_google_places, "yelp": parse_yelp}

# ---------------------------------------------------------------- fixture provider

_NOISE = frozenset({"llc", "inc", "co", "corp", "company", "the", "and", "ltd", "limited"})


def query_tokens(q: str) -> frozenset[str]:
    s = q.lower().replace("&", " ").replace("'", "")
    return frozenset(t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _NOISE)


@dataclass
class FixtureSignalProvider:
    """Serves `dir/index.json`:

        {"places": {"google_places": {"<name>": "file.json"}, "yelp": {...}},
         "search": {"<phrase>": "file.json"}, "rdap": {"<domain>": "file.json"}}

    A place or search query matches the fixture key whose words (case, punctuation, '&' and
    LLC/Inc insensitive) all appear in the query; the longest such key wins, so "Juniper & Rye
    Bakery Portland" finds "juniper and rye bakery". No match is an empty result, as a real API
    would return. `clock` stamps fetched_at (tests and demo staging pin it).
    """

    dir: Path
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        self.dir = Path(self.dir)
        self.index: dict[str, Any] = json.loads((self.dir / "index.json").read_text("utf-8"))

    def _match(self, table: dict[str, str], query: str) -> str | None:
        q = query_tokens(query)
        best: tuple[int, str] | None = None
        for key, file in table.items():
            k = query_tokens(key)
            if k and k <= q and (best is None or len(k) > best[0]):
                best = (len(k), file)
        return best[1] if best else None

    def _read(self, file: str) -> bytes:
        path = (self.dir / file).resolve()
        if self.dir.resolve() not in path.parents:
            raise SignalError("fixture outside the fixture directory")
        return path.read_bytes()

    def place_search(self, source: str, query: str) -> list[SourceResult]:
        if source not in PLACE_SOURCES:
            raise SignalError(f"unknown source '{source}'")
        file = self._match(self.index.get("places", {}).get(source, {}), query)
        if file is None:
            return []
        body = self._read(file)
        sha, now = hashlib.sha256(body).hexdigest(), self.clock()
        try:
            parsed = _PARSERS[source](body)
        except (ValueError, AttributeError, TypeError):
            raise SignalError(f"{source} response could not be parsed") from None
        return [SourceResult(source, ref, "listing", d, url, now, sha) for ref, d, url in parsed]

    def web_search(self, query: str) -> list[SourceResult]:
        file = self._match(self.index.get("search", {}), query)
        if file is None:
            return []
        body = self._read(file)
        sha, now = hashlib.sha256(body).hexdigest(), self.clock()
        try:
            parsed = parse_search(body)
        except (ValueError, AttributeError, TypeError):
            raise SignalError("search response could not be parsed") from None
        return [SourceResult("search", r, "search_result", d, u, now, sha) for r, d, u in parsed]

    def rdap(self, domain: str) -> SourceResult | None:
        domain = domain.strip().lower()
        if not valid_domain(domain):
            raise SignalError("domain must be a bare domain name like example.com")
        file = self.index.get("rdap", {}).get(domain)
        if file is None:
            return None
        body = self._read(file)
        try:
            got, data = parse_rdap(body)
        except (ValueError, AttributeError, TypeError):
            raise SignalError("rdap response could not be parsed") from None
        if got != domain:
            raise SignalError("rdap response is for a different domain")
        url = f"https://rdap.org/domain/{domain}"
        return SourceResult(
            "rdap", domain, "domain_registration", data, url, self.clock(),
            hashlib.sha256(body).hexdigest(),
        )  # fmt: skip


US_FIXTURES_DIR = Path(__file__).resolve().parent / "demo_fixtures" / "us"


def demo_signal_provider(
    dir: Path | None = None, clock: Callable[[], datetime] | None = None
) -> FixtureSignalProvider:
    """The packaged US demo set (three borrowers and a decoy), or another fixture directory."""
    p = FixtureSignalProvider(dir or US_FIXTURES_DIR)
    if clock is not None:
        p.clock = clock
    return p
