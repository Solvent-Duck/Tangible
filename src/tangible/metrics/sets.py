"""Metric sets. Policy and assessments pin a set id, never loose (key, version) pairs."""

from tangible.metrics.registry import REGISTRY

# Sets are append-only like versions: never edit a released set; add "ms-2".
METRIC_SETS: dict[str, tuple[tuple[str, int], ...]] = {
    "ms-1": (("net_revenue", 1), ("refund_rate", 1)),
    "ms-2": (
        ("net_revenue", 1),
        ("refund_rate", 1),
        ("gross_margin_est", 1),
        ("repeat_customer_share", 1),
        ("days_of_inventory", 1),
        ("aged_stock_share", 1),
        ("cash_runway", 1),
        ("payables_discipline", 1),
        ("payout_lag", 1),
        ("history_days", 1),
        ("payables_days", 1),
        ("revenue_growth_30v30", 1),
        ("revenue_slope_13w", 1),
        ("revenue_break_26w", 1),
        ("refund_rate_change_30v30", 1),
        ("ad_spend_ratio_change_30v30", 1),
        ("revenue_yoy_90d", 1),
        ("payout_lag_change_30v30", 1),
        ("payout_lag_slope_13w", 1),
        ("days_of_inventory_change_30v30", 1),
        ("days_of_inventory_slope_13w", 1),
        ("payables_days_change_30v30", 1),
        ("payables_days_slope_13w", 1),
    ),
}


def metric_set(set_id: str) -> tuple[tuple[str, int], ...]:
    members = METRIC_SETS[set_id]
    for key, version in members:
        if (key, version) not in REGISTRY:
            raise KeyError(f"metric set {set_id}: metric {key} v{version} is not registered")
    return members
