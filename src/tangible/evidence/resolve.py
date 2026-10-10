"""Resolve an evidence ref to the raw bytes it points at, for the workbench trace view.

The stored payload's sha256 is always re-verified against evidence_pull before anything is read
out of it. Locator forms:
  row=N            line N (1-based) of a line-oriented payload (synthetic pulls)
  invoice=<ref>    the order's lines in a stored order-lines CSV
  statement        an uploaded document (described, not dumped)
  record=<id>      one JSON record of a connector pull (compact JSON)
"""

import csv
import hashlib
import io
import json
import re
from typing import Any

from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.ledger.evidence import EvidenceRef


class EvidenceIntegrityError(Exception):
    """Stored evidence no longer hashes to the sha256 recorded when it was pulled."""


def resolve_line(conn: Conn, store: EvidenceStore, ref: EvidenceRef) -> str | None:
    """The raw line (or JSON record) `ref` points at, read from the stored pull payload after
    verifying the payload's sha256 against evidence_pull. None if the locator can't be
    resolved; raises EvidenceIntegrityError on a hash mismatch."""
    row = conn.execute(
        "SELECT source, s3_key, sha256 FROM evidence_pull WHERE id = %s", (ref.pull_id,)
    ).fetchone()
    if row is None:
        return None
    source, key, sha = str(row[0]), str(row[1]), str(row[2])
    data = store.get(key)
    if hashlib.sha256(data).hexdigest() != sha:
        raise EvidenceIntegrityError(
            f"evidence pull {ref.pull_id} ({key}): stored bytes do not match recorded sha256"
        )
    kind, _, arg = ref.locator.partition("=")
    if kind == "row" and arg.isdigit():
        return _line(data, int(arg))
    if kind == "invoice" and arg:
        return _order_lines(data, arg)
    if ref.locator == "statement" and source == "document_upload":
        return f"uploaded document {_upload_name(key)}, {len(data)} bytes"
    if kind == "record" and arg:
        return _record(data, arg)
    return None


def _line(data: bytes, n: int) -> str | None:
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline is a terminator, not an empty line
    return lines[n - 1] if 1 <= n <= len(lines) else None


def _order_lines(data: bytes, invoice: str) -> str | None:
    text = data.decode("utf-8", errors="replace")
    raw = text.splitlines()
    if not raw:
        return None
    header = next(csv.reader([raw[0]]))
    if "invoice" not in header:
        return None
    idx = header.index("invoice")
    out = []
    for line in raw[1:]:
        if line and next(csv.reader(io.StringIO(line)), [])[idx : idx + 1] == [invoice]:
            out.append(line)
    return "\n".join(out) if out else None


def _upload_name(key: str) -> str:
    base = key.rsplit("/", 1)[-1]
    return re.sub(r"^[0-9a-f]{16}-", "", base)  # store_upload prefixes the content hash


def _record_id(d: dict[str, Any]) -> str | None:
    for k in ("id", "transaction_id"):
        if isinstance(d.get(k), str | int):
            return str(d[k])
    if "balances" in d and isinstance(d.get("account_id"), str):
        return str(d["account_id"])  # a Plaid balance record
    return None


def _find(node: Any, wanted: str) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if _record_id(node) == wanted:
            return node
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        hit = _find(child, wanted)
        if hit is not None:
            return hit
    return None


def _record(data: bytes, record_id: str) -> str | None:
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    hit = _find(doc, record_id)
    return None if hit is None else json.dumps(hit, separators=(",", ":"), ensure_ascii=False)
