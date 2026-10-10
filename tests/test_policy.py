from pathlib import Path

import pytest

from tangible.policy import load_policy

POLICIES = Path(__file__).parent.parent / "policies"


def test_default_policy_loads_with_stable_hash():
    a = load_policy(POLICIES / "default.yaml")
    b = load_policy(POLICIES / "default.yaml")
    assert a.version == "0.2.2"
    assert a.sha256 == b.sha256 and len(a.sha256) == 64
    assert a.spec["knockout"]["open_finding_severity"] == "hold"


def test_policy_without_version_rejected(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("bands: []\n")
    with pytest.raises(ValueError):
        load_policy(bad)
