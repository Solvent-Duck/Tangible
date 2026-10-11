"""Check sets. Policy and assessments pin a set id, never loose (key, version) pairs."""

from tangible.checks.registry import REGISTRY

# Sets are append-only like versions: never edit a released set; add "cs-2".
CHECK_SETS: dict[str, tuple[tuple[str, int], ...]] = {
    "cs-1": (
        ("payouts_vs_deposits", 1),
        ("books_vs_processor", 1),
        ("orders_vs_payouts", 1),
        ("inventory_flow", 1),
        ("payables_vs_bank", 1),
        ("stated_vs_observed", 1),
        ("time_gaps", 1),
        ("stale_connection", 1),
        ("partial_coverage", 1),
        ("short_history", 1),
        ("revoked_mid_review", 1),
        ("backdated_edits", 1),
        ("round_number_pattern", 1),
        ("self_transfers_as_revenue", 1),
        ("unexplained_recurring_debit", 1),
    ),
}
CHECK_SETS["cs-2"] = (*CHECK_SETS["cs-1"], ("document_mismatch", 1), ("document_file_traits", 1))
# cs-3: time_gaps v2 (bank holidays, volume-aware, episode-based) replaces v1.
CHECK_SETS["cs-3"] = tuple(
    ("time_gaps", 2) if member == ("time_gaps", 1) else member for member in CHECK_SETS["cs-2"]
)
# cs-4: books_vs_processor v2 (waits for payouts to settle) replaces v1.
CHECK_SETS["cs-4"] = tuple(
    ("books_vs_processor", 2) if member == ("books_vs_processor", 1) else member
    for member in CHECK_SETS["cs-3"]
)


def check_set(set_id: str) -> tuple[tuple[str, int], ...]:
    members = CHECK_SETS[set_id]
    for key, version in members:
        if (key, version) not in REGISTRY:
            raise KeyError(f"check set {set_id}: check {key} v{version} is not registered")
    return members
