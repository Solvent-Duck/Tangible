"""Momentum registered over health metrics (see `momentum.register_momentum_for`)."""

from tangible.metrics import health as health  # noqa: F401  (base metrics must be registered)
from tangible.metrics.momentum import register_momentum_for

for _key in ("payout_lag", "days_of_inventory", "payables_days"):
    register_momentum_for(_key, 1)
