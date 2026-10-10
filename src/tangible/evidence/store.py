"""Raw evidence storage: write-once blobs addressed by key.

The production implementation is S3 with Object Lock (governance mode); it is not built here.
"""

import hashlib
from pathlib import Path, PurePosixPath
from typing import Protocol


class EvidenceConflict(Exception):
    """A key already holds different bytes."""


class EvidenceStore(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Store `data` under `key`; returns its sha256 hex. Write-once."""
        ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class LocalEvidenceStore:
    """Filesystem store. `put` of an existing key succeeds only with identical bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        p = PurePosixPath(key)
        if not key or p.is_absolute() or ".." in p.parts or "\\" in key or "\x00" in key:
            raise ValueError(f"invalid evidence key: {key!r}")
        return self.root.joinpath(*p.parts)

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        sha = hashlib.sha256(data).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise EvidenceConflict(f"{key} already exists with different content")
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            return self.put(key, data)
        return sha

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
