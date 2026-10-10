"""Resolve an evidence ref to the raw bytes it points at, for the workbench trace view.

Interface fixed in Wave 4 prep; WP4C implements it (including making synthetic pulls' stored
payloads match their evidence_pull sha256 so `row=N` locators resolve).
"""

from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.ledger.evidence import EvidenceRef


def resolve_line(conn: Conn, store: EvidenceStore, ref: EvidenceRef) -> str | None:
    """The raw line (or JSON record) `ref` points at, read from the stored pull payload after
    verifying the payload's sha256 against evidence_pull. None if the locator can't be
    resolved; raises EvidenceIntegrityError (defined by WP4C) on a hash mismatch."""
    raise NotImplementedError("WP4C")
