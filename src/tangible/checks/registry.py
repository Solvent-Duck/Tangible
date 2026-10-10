"""Integrity-check registry. A check compares sources and returns findings, never a band."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from tangible.ledger.types import Ledger

Severity = Literal["info", "review", "hold"]
Pillar = Literal["health", "momentum", "integrity"]
Direction = Literal["raises_risk", "lowers_risk"]


@dataclass(frozen=True)
class FindingDraft:
    """What a check saw. `evidence_refs` holds `str(EvidenceRef)` values; `facts` holds the
    values the rule compared (Decimals stringified), for "Band C because..." explanations."""

    check_key: str
    check_version: int
    severity: Severity
    pillar: Pillar
    summary: str
    evidence_refs: tuple[str, ...] = field(default=())
    direction: Direction = "raises_risk"
    # A plain dict excluded from hash, rather than a tuple of pairs: consumers want key lookup
    # and JSON, and drafts are never used as dict keys or set members.
    facts: Mapping[str, str] = field(default_factory=dict, hash=False)


CheckFn = Callable[[Ledger, datetime], list[FindingDraft]]


@dataclass(frozen=True)
class CheckSpec:
    key: str
    version: int
    fn: CheckFn


REGISTRY: dict[tuple[str, int], CheckSpec] = {}


def check(key: str, version: int) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        if (key, version) in REGISTRY:
            raise ValueError(f"check {key} v{version} already registered")
        REGISTRY[(key, version)] = CheckSpec(key, version, fn)
        return fn

    return register


def run_check(key: str, version: int, ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    return REGISTRY[(key, version)].fn(ledger, as_of)
