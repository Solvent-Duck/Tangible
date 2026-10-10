"""S3 evidence store: write-once objects under Object Lock (governance mode), SSE-KMS.

Governance mode (not compliance) so a valid deletion request can still remove files. Writes use
`IfNoneMatch="*"`: S3 refuses to overwrite an existing key, so the store is write-once even
without relying on the lock alone. A repeat `put` of identical bytes succeeds; different bytes
raise EvidenceConflict.
"""

import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from tangible.evidence.store import EvidenceConflict

DEFAULT_LOCK_DAYS = 365 * 7
_CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


def _code(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


class S3EvidenceStore:
    """`kms_key_id` is a KMS key id/ARN, or a function from tenant id (the first path segment of
    the key) to that tenant's key id. Pass a client to inject a fake in tests."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        client: Any = None,
        object_lock_days: int = DEFAULT_LOCK_DAYS,
        kms_key_id: str | Callable[[str], str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client
        self.object_lock_days = object_lock_days
        self.kms_key_id = kms_key_id
        self._now = now

    def _key(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/") or "\x00" in key:
            raise ValueError(f"invalid evidence key: {key!r}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _kms(self, key: str) -> str | None:
        k = self.kms_key_id
        return k(key.split("/", 1)[0]) if callable(k) else k

    def put(self, key: str, data: bytes) -> str:
        digest = hashlib.sha256(data).digest()
        sha = digest.hex()
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._key(key),
            "Body": data,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": base64.b64encode(digest).decode(),
            "ObjectLockMode": "GOVERNANCE",
            "ObjectLockRetainUntilDate": self._now() + timedelta(days=self.object_lock_days),
            "IfNoneMatch": "*",
        }
        kms = self._kms(key)
        if kms:
            params["ServerSideEncryption"] = "aws:kms"
            params["SSEKMSKeyId"] = kms
        try:
            self.client.put_object(**params)
        except Exception as e:
            if _code(e) not in _CONFLICT_CODES:
                raise
            if hashlib.sha256(self.get(key)).hexdigest() != sha:
                raise EvidenceConflict(f"{key} already exists with different content") from None
        return sha

    def get(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return bytes(resp["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if _code(e) in _MISSING_CODES:
                return False
            raise
        return True
