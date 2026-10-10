import json
from datetime import date
from decimal import Decimal

import pytest

from tangible.connectors.base import ConnectorAccount
from tangible.connectors.normalize import REMOVED, normalize
from tangible.connectors.plaid import PlaidConnector
from tangible.connectors.pull import load_account, run_pull
from tangible.db.repo import load_ledger
from tangible.db.session import tenant_session
from tangible.evidence.resolve import resolve_line
from tangible.evidence.store import LocalEvidenceStore
from tangible.ledger.evidence import EvidenceRef
from test_connectors_common import T0, T1, TOKEN, Script, new_account, ok, secrets

ACCT = ConnectorAccount("a", "t", "b", "c", "plaid", "mem:token")


def connector(script: Script) -> PlaidConnector:
    return PlaidConnector(
        script.client(), secrets(), "mem:cid", "mem:csecret", sleep=lambda s: None
    )


def test_pagination_cursor_and_records() -> None:
    s = Script(ok("plaid_sync_initial_1.json"), ok("plaid_sync_initial_2.json"),
               ok("plaid_balance.json"))  # fmt: skip
    res = connector(s).pull(ACCT, {})
    bodies = s.bodies()
    assert "cursor" not in bodies[0] and bodies[1]["cursor"] == "cur-1"  # has_more followed
    assert [r.url.path for r in s.requests] == [
        "/transactions/sync",
        "/transactions/sync",
        "/accounts/balance/get",
    ]
    assert bodies[0]["access_token"] == TOKEN
    assert res.next_cursor == {"transactions": "cur-2"}
    assert [r.kind for r in res.records] == ["txn_added"] * 4 + ["balance"]
    env = json.loads(res.raw)  # every response embedded exactly, token never echoed
    assert env["provider"] == "plaid" and len(env["streams"]["transactions"]) == 2
    assert TOKEN.encode() not in res.raw


def test_incremental_pull_starts_from_saved_cursor() -> None:
    s = Script(ok("plaid_sync_update.json"), ok("plaid_balance.json"))
    res = connector(s).pull(ACCT, {"transactions": "cur-2"})
    assert s.bodies()[0]["cursor"] == "cur-2"
    assert res.next_cursor == {"transactions": "cur-3"}


def test_normalize_direction_amount_pending_balance() -> None:
    s = Script(ok("plaid_sync_initial_1.json"), ok("plaid_sync_initial_2.json"),
               ok("plaid_balance.json"))  # fmt: skip
    rows = normalize(ACCT, connector(s).pull(ACCT, {}).records, T0)
    by_ref = {r.values["source_ref"]: r for r in rows}
    assert "tx-pending" not in by_ref  # pending skipped
    out, inn = by_ref["tx-1"].values, by_ref["tx-2"].values
    assert (out["direction"], out["amount"], out["counterparty"]) == (
        "out",
        Decimal("25.50"),
        "Acme Supplies",
    )
    assert (inn["direction"], inn["amount"]) == ("in", Decimal("1200.00"))  # positive, direction in
    assert out["posted_on"] == date(2026, 3, 2) and out["observed_at"] == T0
    assert by_ref["tx-1"].locator == "record=tx-1"
    bal = by_ref["acct-1@2026-03-10"]
    assert bal.table == "account_balance" and bal.values["balance"] == Decimal("4200.25")


def test_modified_and_removed_normalize() -> None:
    s = Script(ok("plaid_sync_update.json"), ok("plaid_balance.json"))
    rows = normalize(ACCT, connector(s).pull(ACCT, {"transactions": "cur-2"}).records, T1)
    by_ref = {r.values["source_ref"]: r.values for r in rows if r.table == "bank_txn"}
    assert by_ref["tx-1"]["amount"] == Decimal("27.50")
    assert by_ref["tx-2"]["amount"] == 0 and by_ref["tx-2"]["description"] == REMOVED


@pytest.mark.db
def test_modification_is_new_row_and_known_picks_revision(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "plaid")
    store = LocalEvidenceStore(tmp_path)
    s1 = Script(ok("plaid_sync_initial_1.json"), ok("plaid_sync_initial_2.json"),
                ok("plaid_balance.json"))  # fmt: skip
    s2 = Script(ok("plaid_sync_update.json"), ok("plaid_balance.json"))
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        acct = load_account(conn, acct.id)
        first = run_pull(conn, store, secrets(), acct, connector(s1), T0)
        assert first.status == "complete" and first.rows_by_table["bank_txn"] == 3
        cur = conn.execute("SELECT stream, cursor FROM connector_cursor").fetchall()
        assert cur == [("transactions", "cur-2")]  # cursor saved
        second = run_pull(conn, store, secrets(), acct, connector(s2), T1)
        assert second.status == "complete"
    assert s2.bodies()[0]["cursor"] == "cur-2"  # next pull resumed from the saved cursor
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        ledger = load_ledger(conn, acct.borrower_id)
        refs = [t.source_ref for t in ledger.bank_txns]
        assert refs.count("tx-1") == 2 and refs.count("tx-2") == 2  # new rows, none overwritten
        then, now = ledger.known(T0), ledger.known(T1)
        t1_then = next(t for t in then.bank_txns if t.source_ref == "tx-1")
        t1_now = next(t for t in now.bank_txns if t.source_ref == "tx-1")
        assert (t1_then.amount, t1_now.amount) == (Decimal("25.50"), Decimal("27.50"))
        assert next(t for t in then.bank_txns if t.source_ref == "tx-2").amount == Decimal(
            "1200.00"
        )
        gone = next(t for t in now.bank_txns if t.source_ref == "tx-2")
        assert gone.amount == 0 and gone.description == REMOVED
        # the evidence ref of the revised row resolves to its record in the second pull's payload
        assert t1_now.evidence is not None and t1_now.evidence.pull_id == second.pull_id
        line = resolve_line(conn, store, EvidenceRef(t1_now.evidence.pull_id, "record=tx-1"))
        assert line is not None and json.loads(line)["amount"] == 27.5
