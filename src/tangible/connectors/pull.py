"""Run one connector pull: fetch, store the raw response, record the pull, write ledger rows.

A fetch is an event: every run inserts a new evidence_pull (successful or failed). Re-pulling
unchanged data therefore inserts a new pull and new, duplicate-looking ledger rows whose
observed_at (the fetch time) is later; the time rule collapses them by source_ref. Callers that
want to avoid the noise should pull incrementally (cursors do this for Plaid and Rutter).

Call this inside `tenant_session`; on failure the failed pull and `connection.last_error` are
committed and a failed PullOutcome is returned (nothing is raised, so the session commits).
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from tangible.connectors.base import Connector, ConnectorAccount
from tangible.connectors.normalize import LedgerRow, normalize
from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.secrets import SecretStore, redact


class ConnectionRevoked(Exception):
    """The borrower revoked consent; no pull is attempted."""


@dataclass
class PullOutcome:
    pull_id: str
    status: str  # 'complete' | 'failed'
    rows_by_table: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def load_account(conn: Conn, account_id: str) -> ConnectorAccount:
    row = conn.execute(
        "SELECT id, tenant_id, borrower_id, connection_id, provider, secret_ref, "
        "external_account_id FROM connector_account WHERE id = %s",
        (account_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"connector account {account_id} not found")
    return ConnectorAccount(
        id=str(row[0]),
        tenant_id=str(row[1]),
        borrower_id=str(row[2]),
        connection_id=str(row[3]),
        provider=str(row[4]),
        secret_ref=str(row[5]),
        external_account_id=row[6],
    )


_PULL_SQL = (
    "INSERT INTO evidence_pull (id, tenant_id, borrower_id, source, connection_id, fetched_at, "
    "s3_key, sha256, byte_size, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _insert_rows(
    conn: Conn, account: ConnectorAccount, pull_id: str, rows: list[LedgerRow]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        cols = [*r.values, "tenant_id", "borrower_id", "evidence_pull_id", "evidence_locator"]
        vals = [
            *r.values.values(), account.tenant_id, account.borrower_id, pull_id, r.locator
        ]  # fmt: skip
        conn.execute(
            f"INSERT INTO {r.table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",  # noqa: S608
            vals,
        )
        counts[r.table] = counts.get(r.table, 0) + 1
    return counts


def run_pull(
    conn: Conn,
    store: EvidenceStore,
    secrets: SecretStore,
    account: ConnectorAccount,
    connector: Connector,
    now: datetime,
) -> PullOutcome:
    row = conn.execute(
        "SELECT revoked_at FROM connection WHERE id = %s", (account.connection_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"connection {account.connection_id} not found")
    if row[0] is not None:  # revocation stops pulls at once: no HTTP call, no pull row
        raise ConnectionRevoked(f"connection {account.connection_id} was revoked at {row[0]}")

    since = {
        str(s): str(c)
        for s, c in conn.execute(
            "SELECT stream, cursor FROM connector_cursor WHERE connector_account_id = %s",
            (account.id,),
        )
    }
    prefix = f"{account.tenant_id}/{account.borrower_id}/pulls"
    pull_id = str(uuid.uuid4())

    try:
        result = connector.pull(account, since)
        rows = normalize(account, result.records, now)
    except Exception as e:
        try:
            known = [secrets.get(account.secret_ref)]
        except Exception:
            known = []
        msg = redact(f"{type(e).__name__}: {e}", known)[:1000]
        payload = json.dumps({"error": msg}).encode()
        key = f"{prefix}/{pull_id}.json"
        sha = store.put(key, payload)
        with conn.transaction():
            conn.execute(
                _PULL_SQL,
                (pull_id, account.tenant_id, account.borrower_id, account.provider,
                 account.connection_id, now, key, sha, len(payload), "failed"),
            )  # fmt: skip
            conn.execute("UPDATE connection SET last_error = %s WHERE id = %s",
                         (msg, account.connection_id))  # fmt: skip
        return PullOutcome(pull_id, "failed", error=msg)

    key = f"{prefix}/{pull_id}.json"
    sha = store.put(key, result.raw)
    with conn.transaction():
        conn.execute(
            _PULL_SQL,
            (pull_id, account.tenant_id, account.borrower_id, account.provider,
             account.connection_id, now, key, sha, len(result.raw), "complete"),
        )  # fmt: skip
        counts = _insert_rows(conn, account, pull_id, rows)
        for stream, cursor in result.next_cursor.items():
            conn.execute(
                "INSERT INTO connector_cursor (tenant_id, connector_account_id, stream, cursor, "
                "updated_at) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (connector_account_id, stream) "
                "DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = EXCLUDED.updated_at",
                (account.tenant_id, account.id, stream, cursor, now),
            )
        conn.execute(
            "UPDATE connection SET last_success_at = %s, last_error = NULL WHERE id = %s",
            (now, account.connection_id),
        )
    return PullOutcome(pull_id, "complete", counts)
