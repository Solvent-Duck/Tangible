"""Fixture-backed provider for tests and demos. Never touches the network."""

from collections.abc import Mapping
from pathlib import Path

from tangible.web.provider import AllowListedProvider, FixtureFetcher, SourceAdapter
from tangible.web.sources import DEFAULT_SOURCES


def fixture_provider(
    dir: Path, sources: Mapping[str, SourceAdapter] | None = None
) -> AllowListedProvider:
    return AllowListedProvider(sources or DEFAULT_SOURCES, FixtureFetcher(dir))
