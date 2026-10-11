"""Global macro signals (migration 0010): dated public facts that apply to every borrower.

Reads work from any session (the app role may SELECT). Writes need the owning role
(`admin_connect`): only the trusted loader adds rows, never a tenant or an agent.
"""

import hashlib
import json
from datetime import datetime
from typing import Any

from tangible.db.session import Conn
from tangible.db.signals import canonical_payload
from tangible.ledger.types import MacroSignal


def load_macro_signals(conn: Conn) -> tuple[MacroSignal, ...]:
    """Every macro row of every revision; `Ledger.known` applies the time rule."""
    rows = conn.execute(
        "SELECT source, source_ref, kind, payload, observed_at, occurred_at, url, sha256 "
        "FROM macro_signal ORDER BY observed_at, source, source_ref, id"
    ).fetchall()
    return tuple(
        MacroSignal(
            source=r[0],
            source_ref=r[1],
            kind=r[2],
            payload_json=canonical_payload(r[3]),
            observed_at=r[4],
            occurred_at=r[5],
            url=r[6],
            sha256=r[7],
        )  # fmt: skip
        for r in rows
    )


def insert_macro_signal(
    admin: Conn,
    *,
    source: str,
    source_ref: str,
    kind: str,
    payload: dict[str, Any],
    observed_at: datetime,
    occurred_at: datetime | None = None,
    url: str | None = None,
) -> bool:
    """Append one macro row as the owning role. Returns False when this exact content is already
    on file (same source, source_ref and payload hash): re-running a load adds nothing."""
    sha = hashlib.sha256(canonical_payload(payload).encode()).hexdigest()
    row = admin.execute(
        "INSERT INTO macro_signal (source, source_ref, kind, payload, url, sha256, occurred_at, "
        "observed_at) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s) "
        "ON CONFLICT (source, source_ref, sha256) DO NOTHING RETURNING id",
        (source, source_ref, kind, json.dumps(payload), url, sha, occurred_at, observed_at),
    ).fetchone()  # fmt: skip
    return row is not None
