"""Metric sets. Policy and assessments pin a set id, never loose (key, version) pairs."""

from tangible.metrics.registry import REGISTRY

# Sets are append-only like versions: never edit a released set; add "ms-2".
METRIC_SETS: dict[str, tuple[tuple[str, int], ...]] = {
    "ms-1": (("net_revenue", 1), ("refund_rate", 1)),
}


def metric_set(set_id: str) -> tuple[tuple[str, int], ...]:
    members = METRIC_SETS[set_id]
    for key, version in members:
        if (key, version) not in REGISTRY:
            raise KeyError(f"metric set {set_id}: metric {key} v{version} is not registered")
    return members
