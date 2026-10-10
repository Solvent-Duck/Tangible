"""Source adapters. URL templates and domains below are CONFIGURATION: the endpoints, markup and
terms of use of each real service must be confirmed (and, where required, a licence or API key
obtained) before live fetching is enabled. Fixtures are the only thing exercised in tests/CI.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from tangible.web.provider import SourceAdapter, WebItem, extract_text, make_item


@dataclass(frozen=True)
class PageAdapter:
    """Generic adapter: one item per fetched page, with visible text extracted."""

    name: str
    domains: tuple[str, ...]
    templates: tuple[str, ...]  # may use {q} (url-encoded query) and {slug}

    def urls(self, query: str) -> list[str]:
        slug = quote(re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-"))
        return [t.format(q=quote_plus(query), slug=slug) for t in self.templates]

    def parse(self, url: str, body: bytes) -> list[WebItem]:
        title, text = extract_text(body.decode("utf-8", errors="replace"))
        return [make_item(self.name, url, body, title, text)] if text or title else []


@dataclass(frozen=True)
class CompaniesRegistryAdapter(PageAdapter):
    """UK Companies House. A query that is a company number also fetches officers and charges
    (charges are the UK analogue of UCC filings); a name query runs the company search."""

    def urls(self, query: str) -> list[str]:
        q = query.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{8}", q):
            base = f"https://find-and-update.company-information.service.gov.uk/company/{q}"
            return [base, f"{base}/officers", f"{base}/charges"]
        return super().urls(query)


reviews = PageAdapter(
    "reviews", ("www.trustpilot.com",), ("https://www.trustpilot.com/search?query={q}",)
)
marketplace = PageAdapter("marketplace", ("www.ebay.com",), ("https://www.ebay.com/usr/{slug}",))
companies_registry = CompaniesRegistryAdapter(
    "companies_registry",
    ("find-and-update.company-information.service.gov.uk",),
    ("https://find-and-update.company-information.service.gov.uk/search/companies?q={q}",),
)
court_records = PageAdapter(
    "court_records",
    ("caselaw.nationalarchives.gov.uk",),
    ("https://caselaw.nationalarchives.gov.uk/search?query={q}",),
)
trademarks = PageAdapter(
    "trademarks",
    ("trademarks.ipo.gov.uk",),
    ("https://trademarks.ipo.gov.uk/ipo-tmtext/page/Results/1/{q}",),
)

DEFAULT_SOURCES: dict[str, SourceAdapter] = {
    a.name: a for a in (reviews, marketplace, companies_registry, court_records, trademarks)
}
SOURCE_NAMES = tuple(DEFAULT_SOURCES)
