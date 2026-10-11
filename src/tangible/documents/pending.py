"""Read uploaded statements that nobody has read yet.

An upload is stored the moment it arrives (`evidence_pull.fetched_at`); reading it (a tool-less
model call, see reader.py) comes later. The lender had the document when it arrived, so the
extract is observed at the upload's `fetched_at`, not at the time the reader ran. The time rule
still holds: a case whose as_of is before the upload cannot see the extract, and one dated after
it can, with no dependence on how late the reader happened to run.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from tangible.agent.llm import ModelClient
from tangible.db.repo import insert_statement
from tangible.db.session import Conn
from tangible.documents.reader import ExtractionError, read_statement
from tangible.documents.upload import SOURCE
from tangible.evidence.store import EvidenceStore
from tangible.ledger.evidence import EvidenceRef

MAX_ERROR = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class ReadOutcome:
    pull_id: str
    ok: bool
    error: str | None = None  # a short plain reason; never document text


def source_ref_for(pull_id: str) -> str:
    """The same ref the agent's read_document tool uses, so re-reads are revisions of one row."""
    return f"statement:{pull_id}"


def _short(e: BaseException) -> str:
    raw = str(e) if isinstance(e, ExtractionError) else f"{type(e).__name__}: {e}"
    return _CTRL.sub(" ", raw).strip()[:MAX_ERROR]


def pending_pulls(conn: Conn, borrower_id: str) -> list[tuple[str, str, datetime]]:
    """(pull_id, s3_key, fetched_at) of this borrower's uploads that have no extract yet."""
    rows = conn.execute(
        "SELECT p.id, p.s3_key, p.fetched_at FROM evidence_pull p "
        "WHERE p.borrower_id = %s AND p.source = %s AND p.status = 'complete' "
        "AND NOT EXISTS (SELECT 1 FROM document_extract d WHERE d.evidence_pull_id = p.id) "
        "ORDER BY p.fetched_at, p.id",
        (borrower_id, SOURCE),
    ).fetchall()
    return [(str(r[0]), str(r[1]), r[2]) for r in rows]


def read_pending(
    conn: Conn,
    store: EvidenceStore,
    client: ModelClient,
    tenant_id: str,
    borrower_id: str,
) -> list[ReadOutcome]:
    """Read every unread upload; each success appends one document_extract row. A file that
    cannot be read (not a PDF, bad reply, model error) is reported and left unread, so a later
    call retries it. Nothing here follows instructions found in a document."""
    out: list[ReadOutcome] = []
    for pull_id, key, fetched_at in pending_pulls(conn, borrower_id):
        try:
            data = store.get(key)
            st = read_statement(
                client, data,
                source_ref=source_ref_for(pull_id),
                evidence=EvidenceRef(pull_id, "statement"),
                observed_at=fetched_at,
            )  # fmt: skip
            with conn.transaction():  # the reader's model_call row stays even if this fails
                insert_statement(conn, tenant_id, borrower_id, st)
        except Exception as e:  # reader, store or model failures are per-document outcomes
            out.append(ReadOutcome(pull_id, False, _short(e)))
        else:
            out.append(ReadOutcome(pull_id, True))
    return out
