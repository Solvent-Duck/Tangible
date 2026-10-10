import pytest

from tangible.checks.sets import CHECK_SETS, check_set
from tangible.metrics.sets import METRIC_SETS, metric_set


def test_released_sets_resolve():
    assert metric_set("ms-1") == (("net_revenue", 1), ("refund_rate", 1))
    assert check_set("cs-1") == ()


def test_metric_set_with_unregistered_metric_raises(monkeypatch):
    monkeypatch.setitem(METRIC_SETS, "ms-bad", (("net_revenue", 1), ("nope", 9)))
    with pytest.raises(KeyError, match="nope"):
        metric_set("ms-bad")


def test_check_set_with_unregistered_check_raises(monkeypatch):
    monkeypatch.setitem(CHECK_SETS, "cs-bad", (("ghost", 1),))
    with pytest.raises(KeyError, match="ghost"):
        check_set("cs-bad")


def test_unknown_set_id_raises():
    with pytest.raises(KeyError):
        metric_set("ms-999")


def test_ms2_resolves_and_policy_metrics_are_in_it():
    from pathlib import Path

    from tangible.metrics.sets import metric_set
    from tangible.policy import load_policy

    keys = {k for k, _ in metric_set("ms-2")}
    policy = load_policy(Path(__file__).parent.parent / "policies/default.yaml")
    assert policy.parsed is not None and policy.parsed.metric_set == "ms-2"
    used = {c.metric for b in policy.parsed.bands for c in b.when}
    used |= {policy.parsed.limit.base_metric, policy.parsed.limit.history.metric}
    assert used <= keys
