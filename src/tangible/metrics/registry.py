"""Metric registry. A metric is a pure function of (ledger, as_of); new formula, new version."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tangible.ledger.types import Ledger

MetricFn = Callable[[Ledger, datetime], Decimal | None]


@dataclass(frozen=True)
class MetricSpec:
    key: str
    version: int
    fn: MetricFn


REGISTRY: dict[tuple[str, int], MetricSpec] = {}


def metric(key: str, version: int) -> Callable[[MetricFn], MetricFn]:
    def register(fn: MetricFn) -> MetricFn:
        if (key, version) in REGISTRY:
            raise ValueError(f"metric {key} v{version} already registered")
        REGISTRY[(key, version)] = MetricSpec(key, version, fn)
        return fn

    return register


def compute(key: str, version: int, ledger: Ledger, as_of: datetime) -> Decimal | None:
    return REGISTRY[(key, version)].fn(ledger, as_of)
