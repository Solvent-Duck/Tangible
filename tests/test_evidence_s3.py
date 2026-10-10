import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tangible.evidence.s3 import S3EvidenceStore
from tangible.evidence.store import EvidenceConflict

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Enforces IfNoneMatch='*' like S3 does and remembers the params of every call."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.puts.append(kw)
        k = (kw["Bucket"], kw["Key"])
        if kw.get("IfNoneMatch") == "*" and k in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[k] = kw["Body"]
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        class Body:
            def __init__(self, b: bytes) -> None:
                self.b = b

            def read(self) -> bytes:
                return self.b

        return {"Body": Body(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {}


def store(client: FakeS3, **kw: Any) -> S3EvidenceStore:
    return S3EvidenceStore("bkt", "ev", client=client, object_lock_days=30, now=lambda: NOW, **kw)


def test_put_parameters() -> None:
    c = FakeS3()
    sha = store(c, kms_key_id="arn:kms:tenant-key").put("t1/b1/f.json", b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest()
    (p,) = c.puts
    assert p["Bucket"] == "bkt" and p["Key"] == "ev/t1/b1/f.json" and p["Body"] == b"hello"
    assert p["ObjectLockMode"] == "GOVERNANCE"
    assert p["ObjectLockRetainUntilDate"] == NOW + timedelta(days=30)
    assert p["IfNoneMatch"] == "*"
    assert p["ChecksumAlgorithm"] == "SHA256"
    assert p["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    assert p["ServerSideEncryption"] == "aws:kms" and p["SSEKMSKeyId"] == "arn:kms:tenant-key"


def test_per_tenant_kms_key_function() -> None:
    c = FakeS3()
    s = store(c, kms_key_id=lambda tenant: f"key-for-{tenant}")
    s.put("tenantA/x", b"1")
    s.put("tenantB/x", b"1")
    assert [p["SSEKMSKeyId"] for p in c.puts] == ["key-for-tenantA", "key-for-tenantB"]


def test_write_once_identical_ok_different_conflicts() -> None:
    c = FakeS3()
    s = store(c, kms_key_id="k")
    first = s.put("k1", b"one")
    assert s.put("k1", b"one") == first  # identical bytes: returns the sha
    with pytest.raises(EvidenceConflict):
        s.put("k1", b"two")
    assert s.get("k1") == b"one"


def test_get_exists_and_bad_keys() -> None:
    c = FakeS3()
    s = store(c, kms_key_id="k")
    assert not s.exists("a/b")
    s.put("a/b", b"x")
    assert s.exists("a/b") and s.get("a/b") == b"x"
    for bad in ("", "/abs", "a/../b"):
        with pytest.raises(ValueError):
            s.put(bad, b"x")


def test_other_errors_propagate() -> None:
    class Denied(FakeS3):
        def put_object(self, **kw: Any) -> dict[str, Any]:
            raise ClientError("AccessDenied")

    with pytest.raises(ClientError):
        store(Denied(), kms_key_id="k").put("k", b"x")
