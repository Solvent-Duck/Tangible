import json
from decimal import Decimal

import httpx
import pytest

from tangible.connectors.base import ConnectorAccount, ConnectorError
from tangible.connectors.normalize import normalize
from tangible.connectors.pull import load_account, run_pull
from tangible.connectors.rutter import RutterConnector
from tangible.db.repo import load_ledger
from tangible.db.session import tenant_session
from tangible.evidence.resolve import resolve_line
from tangible.evidence.store import LocalEvidenceStore
from tangible.ledger.evidence import EvidenceRef
from test_connectors_common import T0, TOKEN, Script, new_account, ok, secrets

ACCT = ConnectorAccount("a", "t", "b", "c", "rutter", "mem:token")
ALL = ("orders", "payouts", "invoices", "bills")


def connector(script: Script, streams: tuple[str, ...] = ALL) -> RutterConnector:
    return RutterConnector(script.client(), secrets(), "mem:cid", "mem:csecret",
                           streams=streams, sleep=lambda s: None)  # fmt: skip


def full_script() -> Script:
    return Script(ok("rutter_orders_1.json"), ok("rutter_orders_2.json"), ok("rutter_payouts.json"),
                  ok("rutter_invoices.json"), ok("rutter_bills.json"))  # fmt: skip


def test_pagination_and_auth() -> None:
    s = full_script()
    res = connector(s).pull(ACCT, {})
    assert [r.url.path for r in s.requests] == [
        "/versioned/orders",
        "/versioned/orders",
        "/versioned/payouts",
        "/versioned/accounting/invoices",
        "/versioned/accounting/bills",
    ]
    assert "cursor" not in s.requests[0].url.params
    assert s.requests[1].url.params["cursor"] == "pg2"
    assert s.requests[0].url.params["access_token"] == TOKEN
    assert s.requests[0].headers["authorization"].startswith("Basic ")
    assert [r.kind for r in res.records] == [
        "order", "refund", "order", "payout", "invoice_receivable", "invoice_payable",
    ]  # fmt: skip
    assert res.next_cursor["orders"] == "2026-03-02T12:30:00.000Z"  # newest updated_at
    env = json.loads(res.raw)
    assert len(env["streams"]["orders"]) == 2 and TOKEN.encode() not in res.raw


def test_incremental_uses_watermark() -> None:
    s = Script(ok("rutter_orders_2.json"))
    connector(s, ("orders",)).pull(ACCT, {"orders": "2026-03-01T10:00:00.000Z"})
    assert s.requests[0].url.params["updated_at_min"] == "2026-03-01T10:00:00.000Z"


def test_each_record_type_normalised_and_refund_linked() -> None:
    rows = normalize(ACCT, connector(full_script()).pull(ACCT, {}).records, T0)
    by = {(r.table, r.values["source_ref"]): r for r in rows}
    order = by["orders", "ord-1"].values
    assert (order["gross"], order["discounts"], order["tax"], order["currency"]) == (
        Decimal("100.0"),
        Decimal("10.0"),
        Decimal("7.2"),
        "USD",
    )
    assert order["customer_ref"] == "cust-ord-1" and order["observed_at"] == T0
    refund = by["refund", "ref-1"]
    assert refund.values["order_ref"] == "ord-1" and refund.values["amount"] == Decimal("20.0")
    assert refund.values["reason"] == "damaged" and refund.locator == "record=ref-1"
    po = by["payout", "po-1"].values
    assert (po["amount"], po["fees"], str(po["settled_on"])) == (
        Decimal("410.5"),
        Decimal("13.2"),
        "2026-03-06",
    )
    rec, pay = by["invoice", "inv-1"].values, by["invoice", "bill-1"].values
    assert (rec["kind"], rec["counterparty"], rec["paid_on"]) == (
        "receivable",
        "Big Retail Co",
        None,
    )
    assert (pay["kind"], pay["counterparty"], str(pay["paid_on"])) == (
        "payable",
        "Supplier Ltd",
        "2026-03-10",
    )


def test_error_message_has_no_token() -> None:
    s = Script(httpx.Response(401, text=f'{{"error":"bad access_token={TOKEN}"}}'))
    with pytest.raises(ConnectorError) as e:
        connector(s, ("orders",)).pull(ACCT, {})
    assert (
        TOKEN not in str(e.value) and "401" in str(e.value) and "access_token" not in str(e.value)
    )


@pytest.mark.db
def test_pull_and_resolve_refund(test_db_url: str, tmp_path) -> None:
    acct = new_account(test_db_url, "rutter")
    store = LocalEvidenceStore(tmp_path)
    with tenant_session(test_db_url, acct.tenant_id) as conn:
        acct = load_account(conn, acct.id)
        out = run_pull(conn, store, secrets(), acct, connector(full_script()), T0)
        assert out.rows_by_table == {
            "orders": 2, "refund": 1, "payout": 1, "invoice": 2,
        }  # fmt: skip
        ledger = load_ledger(conn, acct.borrower_id)
        refund = ledger.refunds[0]
        assert refund.order_ref in {o.source_ref for o in ledger.orders}
        assert refund.evidence is not None
        line = resolve_line(conn, store, refund.evidence)
        assert line is not None and json.loads(line)["reason"] == "damaged"
        order = next(o for o in ledger.orders if o.source_ref == "ord-1")
        assert order.evidence is not None
        assert resolve_line(conn, store, EvidenceRef(order.evidence.pull_id, "record=nope")) is None
