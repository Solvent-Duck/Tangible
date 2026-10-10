"""Check sets. Policy and assessments pin a set id, never loose (key, version) pairs."""

from tangible.checks.registry import REGISTRY

# Sets are append-only like versions: never edit a released set; add "cs-2".
CHECK_SETS: dict[str, tuple[tuple[str, int], ...]] = {
    "cs-1": (),
}


def check_set(set_id: str) -> tuple[tuple[str, int], ...]:
    members = CHECK_SETS[set_id]
    for key, version in members:
        if (key, version) not in REGISTRY:
            raise KeyError(f"check set {set_id}: check {key} v{version} is not registered")
    return members
