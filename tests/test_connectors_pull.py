import hashlib
import json
import logging

import httpx
import pytest

from tangible.connectors.base import ConnectorAccount, ConnectorError, PullResult
from tangible.connectors.plaid import PlaidConnector
from tangible.connectors.pull import ConnectionRevoked, load_account, run_pull
from tangible.db.session import admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.secrets import redact
from test_connectors_common import T0, T1, TOKEN, Script, new_account, ok, secrets

pytestmark = pytest.mark.db


def plaid(script: Script) -> PlaidConnector:
    return PlaidConnector(
        script.client(), secrets(), "mem:cid", "mem:csecret", sleep=lambda s: None
    )


def happy() -> Script:
    return Script(ok("plaid_sync_initial_1.json"), ok("plaid_sync_initial_2.json"),
                  ok("plaid_balance.json"))  # fmt: skip


def pulls(conn, acct: ConnectorAccount):  # type: ignore[no-untyped-def]
    return conn.execute(
        "SELECT id, status, s3_key, sha256, byte_size, source FROM evidence_pull "
        "WHERE connection_id = %s ORDER BY fetched_at, id",
        (acct.connection_id,),
    ).fetchall()


def test_revoked_connection_refused_without_http(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid", revoked=True)
    s = Script()  # any request would fail the test
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        with pytest.raises(ConnectionRevoked):
            run_pull(conn, LocalEvidenceStore(tmp_path), secrets(), acct, plaid(s), T0)
        assert pulls(conn, acct) == []
    assert s.requests == []


def test_revocation_stops_later_pulls(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    store = LocalEvidenceStore(tmp_path)
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        assert run_pull(conn, store, secrets(), acct, plaid(happy()), T0).status == "complete"
        conn.execute(
            "UPDATE connection SET revoked_at = %s WHERE id = %s", (T1, acct.connection_id)
        )
        with pytest.raises(ConnectionRevoked):
            run_pull(conn, store, secrets(), acct, plaid(Script()), T1)
        assert len(pulls(conn, acct)) == 1


def test_raw_bytes_stored_and_hash_verified(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    store = LocalEvidenceStore(tmp_path)
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        out = run_pull(conn, store, secrets(), acct, plaid(happy()), T0)
        [(pid, status, key, sha, size, source)] = pulls(conn, acct)
        assert (str(pid), status, source) == (out.pull_id, "complete", "plaid")
        data = store.get(key)
        assert hashlib.sha256(data).hexdigest() == sha and len(data) == size
        assert TOKEN not in data.decode()
        assert key == f"{acct.tenant_id}/{acct.borrower_id}/pulls/{out.pull_id}.json"
        assert conn.execute(
            "SELECT last_success_at, last_error FROM connection WHERE id = %s",
            (acct.connection_id,),
        ).fetchone() == (T0, None)


def test_429_then_200_is_retried(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    slept: list[float] = []
    s = Script(httpx.Response(429, headers={"Retry-After": "2"}), ok("plaid_sync_initial_2.json"),
               ok("plaid_balance.json"))  # fmt: skip
    c = PlaidConnector(s.client(), secrets(), "mem:cid", "mem:csecret", sleep=slept.append)
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        out = run_pull(conn, LocalEvidenceStore(tmp_path), secrets(), acct, c, T0)
    assert out.status == "complete" and slept == [2.0] and len(s.requests) == 3


def test_failure_records_failed_pull_and_redacted_error(
    test_db_url: str, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    acct = new_account(test_db_url, "plaid")
    store = LocalEvidenceStore(tmp_path)

    class Boom:
        provider = "plaid"

        def pull(self, account: ConnectorAccount, since: object) -> PullResult:
            raise ConnectorError(f"upstream said no for token {TOKEN}")  # a leaky message

    with caplog.at_level(logging.DEBUG), tenant_session(test_db_url, acct.tenant_id) as conn:
        out = run_pull(conn, store, secrets(), acct, Boom(), T0)
        assert out.status == "failed" and out.error and TOKEN not in out.error
        [(pid, status, key, sha, _, _)] = pulls(conn, acct)
        assert status == "failed" and TOKEN not in store.get(key).decode()
        assert hashlib.sha256(store.get(key)).hexdigest() == sha
        (last_error,) = conn.execute(
            "SELECT last_error FROM connection WHERE id = %s", (acct.connection_id,)
        ).fetchone()
        assert last_error and TOKEN not in last_error and "REDACTED" in last_error
        for t in ("orders", "refund", "payout", "bank_txn", "invoice", "account_balance"):
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,)  # noqa: S608
        assert conn.execute("SELECT count(*) FROM connector_cursor").fetchone() == (0,)
    assert TOKEN not in caplog.text


def test_http_failure_message_never_contains_token(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    s = Script(*[httpx.Response(503, text=f"oops {TOKEN}") for _ in range(4)])
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        out = run_pull(conn, LocalEvidenceStore(tmp_path), secrets(), acct, plaid(s), T0)
    assert out.status == "failed" and "503" in (out.error or "") and TOKEN not in (out.error or "")


def test_repull_identical_data_is_a_new_pull_with_later_observed_at(
    test_db_url: str, tmp_path
) -> None:
    """A fetch is an event: identical data yields a new pull and new rows whose observed_at
    differs; the time rule (same source_ref) collapses them."""
    acct = new_account(test_db_url, "plaid")
    store = LocalEvidenceStore(tmp_path)
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        a = run_pull(conn, store, secrets(), acct, plaid(happy()), T0)
        b = run_pull(conn, store, secrets(), acct, plaid(happy()), T1)
        assert a.pull_id != b.pull_id and len(pulls(conn, acct)) == 2
        rows = conn.execute(
            "SELECT source_ref, observed_at FROM bank_txn WHERE source_ref = 'tx-1' "
            "ORDER BY observed_at"
        ).fetchall()
        assert [r[1] for r in rows] == [T0, T1]


def test_account_lookup_and_rls(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    other = new_account(test_db_url, "rutter")
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        run_pull(conn, LocalEvidenceStore(tmp_path), secrets(), acct, plaid(happy()), T0)
    with tenant_session(test_db_url, other.tenant_id) as conn:
        for t in ("connector_account", "connector_cursor"):
            (n,) = conn.execute(f"SELECT count(*) FROM {t}").fetchone()  # noqa: S608
            assert n == (1 if t == "connector_account" else 0)  # only its own account
        with pytest.raises(LookupError):
            load_account(conn, acct.id)
        # writing a row for another tenant is refused
        with pytest.raises(Exception, match="row-level security"):
            conn.execute(
                "INSERT INTO connector_cursor (tenant_id, connector_account_id, stream, cursor) "
                "VALUES (%s, %s, 'x', 'y')",
                (acct.tenant_id, acct.id),
            )
    with admin_connect(test_db_url) as admin:  # token never in the DB
        dump = admin.execute("SELECT secret_ref FROM connector_account").fetchall()
        assert all(TOKEN not in r[0] for r in dump)


def test_redact_helper() -> None:
    msg = redact(f"GET /x?access_token={TOKEN}&a=1 Authorization: Bearer abc.def", [TOKEN])
    assert TOKEN not in msg and "abc.def" not in msg and "a=1" in msg
    assert json.loads(redact(json.dumps({"secret": "s3cr3t"}))) == {"secret": "[REDACTED]"}
