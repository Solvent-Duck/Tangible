"""Canonical JSON and the hashes an assessment pins: ledger input and evidence set."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from tangible.db.session import Conn
from tangible.ledger.types import Ledger

_TABLES = (
    "orders", "refunds", "payouts", "bank_txns", "invoices", "inventory_snapshots",
    "account_balances", "connections", "applications", "public_signals",
)  # fmt: skip


def _default(o: Any) -> str:
    if isinstance(o, datetime):
        return (o.astimezone(UTC) if o.tzinfo else o).isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"not canonicalizable: {type(o).__name__}")


def canonical_json(value: Any) -> str:
    """Sorted keys, compact separators; Decimals and dates as strings."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_default
    )


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def ledger_input_hash(known: Ledger) -> str:
    """sha256 over sorted (table, source_ref, observed_at, evidence) of a time-filtered ledger."""
    rows = sorted(
        (table, r.source_ref, _default(r.observed_at), str(r.evidence) if r.evidence else "")
        for table in _TABLES
        for r in getattr(known, table)
    )
    return canonical_hash(rows)


def evidence_set_hash(conn: Conn, known: Ledger) -> str:
    """sha256 over the sorted sha256 values of the evidence pulls the known rows reference."""
    pull_ids = sorted(
        {
            str(r.evidence.pull_id)
            for table in _TABLES
            for r in getattr(known, table)
            if r.evidence is not None
        }
    )
    shas: list[str] = []
    if pull_ids:
        shas = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT sha256 FROM evidence_pull WHERE id = ANY(%s::uuid[])", (pull_ids,)
            ).fetchall()
        )
    return canonical_hash(shas)
