import hashlib
from importlib import resources

import pytest

from tangible.agent import prompts
from tangible.agent.prompts import PROMPT_HASHES, latest, load_prompt


def test_load_and_latest() -> None:
    p = load_prompt("system", 1)
    assert p.key == "system" and p.version == 1
    assert "untrusted" in p.text
    assert latest("system") == p
    assert p.sha256 == PROMPT_HASHES["system@v1"]


def test_unknown_is_key_error() -> None:
    with pytest.raises(KeyError):
        load_prompt("system", 99)
    with pytest.raises(KeyError):
        load_prompt("nope", 1)
    with pytest.raises(KeyError):
        latest("nope")


def test_released_prompts_are_pinned() -> None:
    files = [f for f in resources.files(prompts).iterdir() if f.name.endswith(".md")]
    assert files
    for f in files:
        key = f.name.removesuffix(".md")
        assert key in PROMPT_HASHES, f"{f.name} has no pinned hash"
        assert hashlib.sha256(f.read_bytes()).hexdigest() == PROMPT_HASHES[key], (
            f"{f.name} was edited; add a new @vN file instead"
        )
    assert {f.name.removesuffix(".md") for f in files} == set(PROMPT_HASHES)


def test_investigate_v4_is_the_latest_and_teaches_verification() -> None:
    p = load_prompt("investigate", 4)
    assert p.key == "investigate" and p.version == 4
    assert p.sha256 == PROMPT_HASHES["investigate@v4"] and len(p.sha256) == 64
    assert latest("investigate") == p
    for needle in ("run_analysis", "query_ledger", "record_finding", "UNVERIFIED", "hold"):
        assert needle in p.text
    assert "Checked, nothing found" in p.text
    # older versions stay loadable and unchanged for assessments frozen against them
    assert load_prompt("investigate", 2).sha256 == PROMPT_HASHES["investigate@v2"]
    assert load_prompt("investigate", 3).sha256 == PROMPT_HASHES["investigate@v3"]


def test_investigate_v4_adds_replaces_rounding_web_and_plain_wrap_up() -> None:
    v3, v4 = load_prompt("investigate", 3).text, load_prompt("investigate", 4).text
    assert v3 != v4
    # corrections replace the earlier finding
    assert "`replaces`" in v4 and "near-duplicate" in v4
    # rounding guidance, with the worked example from the live run
    assert "Round sensibly" in v4 and "80.2%" in v4
    # external checks, one lookup per source the provider offers
    from tangible.web.sources import SOURCE_NAMES

    assert "web_lookup" in v4 and "not available" in v4
    for source in ("companies_registry", "court_records", "reviews", "marketplace"):
        assert source in SOURCE_NAMES and source in v4
    # untrusted web content, efficiency and plain-text wrap-up
    assert "injection" in v4
    assert "25 steps" in v4
    assert "The wrap-up is plain text" in v4
    assert "no headings (#)" in v4 and "no bold (**)" in v4


def test_plan_v3_adds_the_standing_external_risk() -> None:
    v2, v3 = load_prompt("plan", 2), load_prompt("plan", 3)
    assert v3.sha256 == PROMPT_HASHES["plan@v3"] and v3.sha256 != v2.sha256
    assert latest("plan") == v3
    assert "External standing and public records" in v3.text
    for needle in ("web_lookup", "companies_registry", "court_records", "reviews", "marketplace"):
        assert needle in v3.text
    assert "untrusted" in v3.text and "may not be available" in v3.text
    assert "External standing" not in v2.text  # released v2 is untouched
