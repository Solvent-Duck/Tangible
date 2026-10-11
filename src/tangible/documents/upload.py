"""Store an uploaded file as write-once evidence plus an evidence_pull row."""

import hashlib
import re
import uuid
from datetime import UTC, datetime

from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore

SOURCE = "document_upload"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def store_upload(
    conn: Conn, store: EvidenceStore, tenant_id: str, borrower_id: str, filename: str, data: bytes
) -> str:
    """Write `data` to the store and record an evidence_pull. Returns the pull id.

    Idempotent per (borrower, sha256): re-uploading identical bytes returns the existing pull.
    The stored key is derived from the content hash, so a filename can never overwrite anything.
    """
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute(
        "SELECT id FROM evidence_pull WHERE borrower_id = %s AND sha256 = %s AND source = %s "
        "ORDER BY fetched_at LIMIT 1",
        (borrower_id, sha, SOURCE),
    ).fetchone()
    if row:
        return str(row[0])
    safe = _UNSAFE.sub("_", filename.rsplit("/", 1)[-1]).strip("._")[:80] or "upload"
    key = f"{tenant_id}/{borrower_id}/uploads/{sha[:16]}-{safe}"
    store.put(key, data)
    pull_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO evidence_pull (id, tenant_id, borrower_id, source, fetched_at, s3_key, "
        "sha256, byte_size, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'complete')",
        (pull_id, tenant_id, borrower_id, SOURCE, datetime.now(UTC), key, sha, len(data)),
    )
    return pull_id


def display_name(s3_key: str) -> str:
    """The sanitised file name in a stored key `<tenant>/<borrower>/uploads/<sha16>-<name>`."""
    base = s3_key.rsplit("/", 1)[-1]
    return base[17:] if len(base) > 17 and base[16] == "-" else base
