import hashlib
from pathlib import Path

import pytest

from tangible.evidence.store import EvidenceConflict, LocalEvidenceStore


def test_put_get_roundtrip(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path)
    sha = store.put("t/b/file.csv", b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest()
    assert store.exists("t/b/file.csv")
    assert store.get("t/b/file.csv") == b"hello"
    assert not store.exists("t/b/other.csv")


def test_put_same_bytes_is_idempotent(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path)
    assert store.put("k", b"x") == store.put("k", b"x")


def test_put_different_bytes_conflicts_and_keeps_original(tmp_path: Path):
    store = LocalEvidenceStore(tmp_path)
    store.put("k", b"one")
    with pytest.raises(EvidenceConflict):
        store.put("k", b"two")
    assert store.get("k") == b"one"


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/abs/path", "", "a\\..\\b"])
def test_bad_keys_rejected(tmp_path: Path, key: str):
    store = LocalEvidenceStore(tmp_path / "root")
    with pytest.raises(ValueError):
        store.put(key, b"x")
    with pytest.raises(ValueError):
        store.get(key)
    with pytest.raises(ValueError):
        store.exists(key)
