import pytest

from tangible.checks.sets import CHECK_SETS, check_set
from tangible.metrics.sets import METRIC_SETS, metric_set


def test_released_sets_resolve():
    assert metric_set("ms-1") == (("net_revenue", 1), ("refund_rate", 1))
    assert len(check_set("cs-1")) == 15


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
    policy = load_policy(Path(__file__).parent.parent / "policies/default-0.3.0.yaml")
    assert policy.parsed is not None and policy.parsed.metric_set == "ms-2"
    used = {c.metric for b in policy.parsed.bands for c in b.when}
    used |= {policy.parsed.limit.base_metric, policy.parsed.limit.history.metric}
    assert used <= keys


def test_cs4_is_cs3_with_books_vs_processor_v2():
    cs3, cs4 = check_set("cs-3"), check_set("cs-4")
    assert ("books_vs_processor", 2) in cs4 and ("books_vs_processor", 1) not in cs4
    assert len(cs4) == len(cs3) == 17
    assert [m for m in cs4 if m[0] != "books_vs_processor"] == [
        m for m in cs3 if m[0] != "books_vs_processor"
    ]
    # Released sets are untouched.
    assert ("books_vs_processor", 1) in check_set("cs-3") and ("time_gaps", 2) in cs4


def test_policies_pin_the_matching_check_sets():
    from pathlib import Path

    from tangible.policy import load_policy

    root = Path(__file__).parent.parent / "policies"
    assert load_policy(root / "default.yaml").parsed.check_set == "cs-7"  # type: ignore[union-attr]
    assert load_policy(root / "default-0.5.0.yaml").parsed.check_set == "cs-6"  # type: ignore[union-attr]
    assert load_policy(root / "default-0.4.0.yaml").parsed.check_set == "cs-5"  # type: ignore[union-attr]
    assert load_policy(root / "default-0.3.0.yaml").parsed.check_set == "cs-4"  # type: ignore[union-attr]
    assert load_policy(root / "default-0.2.2.yaml").parsed.check_set == "cs-3"  # type: ignore[union-attr]


def test_cs7_swaps_only_text_versions_of_cs6():
    cs6, cs7 = check_set("cs-6"), check_set("cs-7")
    assert len(cs7) == len(cs6) == 22
    assert [k for k, _ in cs7] == [k for k, _ in cs6]
    changed = {k: (a, b) for (k, a), (_, b) in zip(cs6, cs7, strict=True) if a != b}
    assert changed == {
        "payouts_vs_deposits": (1, 2), "books_vs_processor": (2, 3), "orders_vs_payouts": (1, 2),
        "payables_vs_bank": (1, 2), "stated_vs_observed": (1, 2), "time_gaps": (2, 3),
        "partial_coverage": (1, 2), "round_number_pattern": (1, 2),
        "self_transfers_as_revenue": (1, 2), "unexplained_recurring_debit": (1, 2),
        "document_mismatch": (1, 2), "review_volume_vs_orders": (1, 2),
    }  # fmt: skip
