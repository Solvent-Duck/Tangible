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
