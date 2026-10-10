"""Integrity-check registry. A check compares sources and returns findings, never a band."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from tangible.ledger.types import Ledger

Severity = Literal["info", "review", "hold"]
Pillar = Literal["health", "momentum", "integrity"]


@dataclass(frozen=True)
class FindingDraft:
    check_key: str
    check_version: int
    severity: Severity
    pillar: Pillar
    summary: str
    evidence_refs: tuple[str, ...] = field(default=())


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
