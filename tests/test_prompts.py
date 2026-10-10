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
