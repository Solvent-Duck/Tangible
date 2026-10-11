from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tangible.policy import load_policy

POLICIES = Path(__file__).parent.parent / "policies"


def test_default_policy_loads_with_stable_hash():
    a = load_policy(POLICIES / "default.yaml")
    b = load_policy(POLICIES / "default.yaml")
    assert a.version == "0.7.0"
    assert a.sha256 == b.sha256 and len(a.sha256) == 64
    assert a.spec["knockout"]["open_finding_severity"] == "hold"


def test_policy_without_version_rejected(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("bands: []\n")
    with pytest.raises(ValueError):
        load_policy(bad)


def test_released_022_file_is_kept_byte_for_byte():
    # Assessments frozen under 0.2.2 carry this hash; replay needs the file to match it.
    old = load_policy(POLICIES / "default-0.2.2.yaml")
    assert old.version == "0.2.2" and old.parsed is not None
    assert old.sha256 == "6b86a19a43e75e1a16de1be775aed211b9a9c678a573d13053fd697687366001"
    assert old.parsed.check_set == "cs-3" and old.parsed.knockout.model_hold_states is None


def test_released_030_file_is_kept_byte_for_byte():
    # Assessments frozen under 0.3.0 carry this hash (default.yaml before 0.4.0).
    old = load_policy(POLICIES / "default-0.3.0.yaml")
    assert old.version == "0.3.0" and old.parsed is not None and old.parsed.uplift is None
    assert old.sha256 == "90140828f0221745894876aa5d97f164a77e4b55b4b29c722d3e2917c1f4dfe3"


def test_040_adds_uplift_on_ms3_cs5_and_keeps_bands_and_limits_of_030():
    old = load_policy(POLICIES / "default-0.3.0.yaml")
    new = load_policy(POLICIES / "default-0.4.0.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert (new.parsed.metric_set, new.parsed.check_set) == ("ms-3", "cs-5")
    assert new.parsed.bands == old.parsed.bands and new.parsed.limit == old.parsed.limit
    assert new.parsed.knockout == old.parsed.knockout
    up = new.parsed.uplift
    assert up is not None and up.max_notches == 1 and up.ceiling == "B"
    assert [(r.name, r.enabled) for r in up.routes] == [
        ("reputation", False), ("loyalty", True), ("stock", True),
    ]  # fmt: skip
    assert up.requires_for == ("reputation",)


def test_030_pins_cs4_and_the_same_metric_set_as_022():
    old = load_policy(POLICIES / "default-0.2.2.yaml")
    new = load_policy(POLICIES / "default-0.3.0.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert new.parsed.check_set == "cs-4" and new.parsed.metric_set == old.parsed.metric_set
    assert new.parsed.knockout.model_hold_states == ("accepted",)
    assert new.sha256 != old.sha256
    # Only the knockout origin rule, the version and the check set differ.
    assert new.parsed.bands == old.parsed.bands and new.parsed.limit == old.parsed.limit


def test_released_050_file_is_kept_byte_for_byte():
    # Assessments frozen under 0.5.0 carry this hash (default.yaml before 0.6.0).
    old = load_policy(POLICIES / "default-0.5.0.yaml")
    assert old.version == "0.5.0" and old.parsed is not None and old.parsed.report is None
    assert old.sha256 == "8fe7d8c7b3d61a4045b45b00de29605a7ca32a6358c791721b979d38c1020bf6"


def test_released_060_file_is_kept_byte_for_byte():
    # Assessments frozen under 0.6.0 carry this hash (default.yaml before 0.7.0).
    old = load_policy(POLICIES / "default-0.6.0.yaml")
    assert old.version == "0.6.0"
    assert old.sha256 == "4ccc8c02f1e2cd04cfd8d067808d285db0907b1dcb83e55b9f2a117f72a85f85"


def test_070_moves_to_cs8_and_keeps_every_rule_of_060():
    old = load_policy(POLICIES / "default-0.6.0.yaml")
    new = load_policy(POLICIES / "default.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert (new.parsed.metric_set, new.parsed.check_set) == ("ms-4", "cs-8")
    for f in ("bands", "limit", "knockout", "uplift", "review_findings", "stress", "report"):
        assert getattr(new.parsed, f) == getattr(old.parsed, f), f


def test_060_writes_dollars_on_cs7_and_keeps_every_rule_of_050():
    old = load_policy(POLICIES / "default-0.5.0.yaml")
    new = load_policy(POLICIES / "default-0.6.0.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert (new.parsed.metric_set, new.parsed.check_set) == ("ms-4", "cs-7")
    for f in ("bands", "limit", "knockout", "uplift", "review_findings", "stress"):
        assert getattr(new.parsed, f) == getattr(old.parsed, f), f
    rep = new.parsed.report
    assert rep is not None and (rep.currency, rep.decimals) == ("USD", 2)


@pytest.mark.parametrize(
    "report", [{"currency": "GBP", "decimals": 2}, {"currency": "USD", "decimals": 3}, {}]
)
def test_report_block_accepts_only_usd_to_two_decimals(tmp_path: Path, report: dict[str, object]):
    spec = yaml.safe_load((POLICIES / "default.yaml").read_text())
    spec["report"] = report
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(spec))
    with pytest.raises(ValueError, match="policy.report"):
        load_policy(bad)


def test_050_adds_the_stressed_limit_on_ms4_cs6_and_keeps_everything_else_of_040():
    old = load_policy(POLICIES / "default-0.4.0.yaml")
    new = load_policy(POLICIES / "default-0.5.0.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert (new.parsed.metric_set, new.parsed.check_set) == ("ms-4", "cs-6")
    for f in ("bands", "limit", "knockout", "uplift", "review_findings"):
        assert getattr(new.parsed, f) == getattr(old.parsed, f), f
    st = new.parsed.stress
    assert st is not None and st.floor == Decimal("0.5")
    assert [(r.when[0].metric, r.when[0].max, r.limit_factor) for r in st.rules] == [
        ("stressed_runway", Decimal(3), Decimal("0.5")),
        ("stressed_runway", Decimal(6), Decimal("0.75")),
        ("stressed_flow_margin", Decimal(0), Decimal("0.75")),
    ]
    assert old.parsed.stress is None
