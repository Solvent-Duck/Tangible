from pathlib import Path

import pytest

from tangible.policy import load_policy

POLICIES = Path(__file__).parent.parent / "policies"


def test_default_policy_loads_with_stable_hash():
    a = load_policy(POLICIES / "default.yaml")
    b = load_policy(POLICIES / "default.yaml")
    assert a.version == "0.3.0"
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


def test_030_pins_cs4_and_the_same_metric_set_as_022():
    old = load_policy(POLICIES / "default-0.2.2.yaml")
    new = load_policy(POLICIES / "default.yaml")
    assert old.parsed is not None and new.parsed is not None
    assert new.parsed.check_set == "cs-4" and new.parsed.metric_set == old.parsed.metric_set
    assert new.parsed.knockout.model_hold_states == ("accepted",)
    assert new.sha256 != old.sha256
    # Only the knockout origin rule, the version and the check set differ.
    assert new.parsed.bands == old.parsed.bands and new.parsed.limit == old.parsed.limit
