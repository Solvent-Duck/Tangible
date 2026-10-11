"""Fixture-backed providers for tests and demos. Never touch the network.

`fixture_provider` is the strict one the tests use: a URL with no fixture is an error.
`demo_provider` serves the packaged demo set (demo_fixtures/) and is what the workbench uses when
TANGIBLE_WEB_PROVIDER=fixtures: lookups are forgiving about how a name is written, and a search
with no fixture returns a clean "No record found" page, as a real registry would.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

from tangible.web.provider import AllowListedProvider, FetchResponse, FixtureFetcher, SourceAdapter
from tangible.web.sources import DEFAULT_SOURCES

DEMO_FIXTURES_DIR = Path(__file__).resolve().parent / "demo_fixtures"

NO_RECORD_TITLE = "No record found"


def fixture_provider(
    dir: Path, sources: Mapping[str, SourceAdapter] | None = None
) -> AllowListedProvider:
    return AllowListedProvider(sources or DEFAULT_SOURCES, FixtureFetcher(dir))


# Legal-form words and joiners that a person or model may include or omit when writing a name.
_NOISE = frozenset({"ltd", "limited", "co", "company", "and"})


def canonical_url(url: str) -> tuple[str, ...]:
    """Case, punctuation, '&' vs 'and' and Ltd/Limited/Co insensitive form of a URL."""
    tokens = re.split(r"[^a-z0-9]+", unquote_plus(url).lower())
    return tuple(t for t in tokens if t and t not in _NOISE)


class DemoFixtureFetcher(FixtureFetcher):
    """FixtureFetcher that matches URLs by `canonical_url` (exact match wins) and answers an
    unmatched, allow-listed URL with a plain "No record found" page instead of an error."""

    def __init__(self, dir: Path) -> None:
        super().__init__(dir)
        self._canon: dict[tuple[str, ...], dict[str, str]] = {}
        for url, entry in self.index.items():
            self._canon.setdefault(canonical_url(url), entry)

    def _entry(self, url: str) -> dict[str, str] | None:
        return self.index.get(url) or self._canon.get(canonical_url(url))

    def _missing(self, url: str) -> FetchResponse:
        host = urlsplit(url).hostname or "this source"  # already checked against the allow-list
        body = (
            f"<!doctype html><html><head><title>{NO_RECORD_TITLE}</title></head><body>"
            f"<h1>{NO_RECORD_TITLE}</h1>"
            f"<p>The search on {host} returned no matching record.</p></body></html>"
        )
        return FetchResponse(url, body.encode("utf-8"))


def demo_provider(index: Path | None = None) -> AllowListedProvider:
    """Provider over a fixture index (a path to index.json, or its directory); defaults to the
    packaged demo set."""
    path = Path(index) if index is not None else DEMO_FIXTURES_DIR
    index_file = path / "index.json" if path.is_dir() else path
    if index_file.name != "index.json" or not index_file.is_file():
        raise FileNotFoundError(f"web fixtures index.json not found at {path}")
    directory = index_file.parent
    return AllowListedProvider(DEFAULT_SOURCES, DemoFixtureFetcher(directory))
