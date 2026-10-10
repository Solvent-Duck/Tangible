from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tangible.ledger import (
    AccountBalance,
    Application,
    BankTxn,
    Connection,
    EvidenceRef,
    InventorySnapshot,
    Invoice,
    Ledger,
    Order,
    Payout,
    Refund,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)
D = date(2026, 2, 1)
D0 = Decimal(0)


def test_evidence_ref_round_trip():
    ref = EvidenceRef("pull-1", "row=7")
    assert str(ref) == "pull-1#row=7"
    assert EvidenceRef.parse(str(ref)) == ref
    assert EvidenceRef.parse("p#a#b") == EvidenceRef("p", "a#b")


@pytest.mark.parametrize("bad", ["", "nohash", "#loc", "pull#"])
def test_evidence_ref_malformed(bad):
    with pytest.raises(ValueError):
        EvidenceRef.parse(bad)


def _rows(ref: str, seen: datetime) -> dict[str, object]:
    return {
        "orders": Order(ref, D0, D0, seen, seen),
        "refunds": Refund(ref, "o", D0, seen, seen),
        "payouts": Payout(ref, "stripe", D0, D0, None, None, seen),
        "bank_txns": BankTxn(ref, "a", D0, "in", None, None, D, seen),
        "invoices": Invoice(ref, "payable", None, D0, D, None, None, seen),
        "inventory_snapshots": InventorySnapshot(ref, "sku", D0, None, D, seen),
        "account_balances": AccountBalance(ref, "a", D0, D, seen),
        "connections": Connection(ref, "shopify", seen, None, None, seen),
        "applications": Application(ref, None, (), (), seen),
    }


TABLES = list(_rows("x", T0))


@pytest.mark.parametrize("table", TABLES)
def test_known_drops_rows_observed_after_as_of(table):
    early = _rows("early", T0)[table]
    late = _rows("late", T0 + timedelta(days=5))[table]
    ledger = Ledger(**{table: (late, early)})
    assert getattr(ledger.known(T0), table) == (early,)
    assert getattr(ledger.known(T0 + timedelta(days=5)), table) == (early, late)


def test_revision_latest_known_wins():
    issued = Invoice("inv1", "receivable", "acme", Decimal(100), D, date(2026, 3, 1), None, T0)
    paid = Invoice(
        "inv1", "receivable", "acme", Decimal(100), D, date(2026, 3, 1), date(2026, 2, 20),
        T0 + timedelta(days=10),
    )  # fmt: skip
    ledger = Ledger(invoices=(paid, issued))
    assert ledger.known(T0 + timedelta(days=1)).invoices == (issued,)
    assert ledger.known(T0 + timedelta(days=10)).invoices == (paid,)


def test_known_ordering_is_deterministic():
    rows = [Order(r, D0, D0, T0, T0) for r in ("b", "a", "c")]
    later = Order("z", D0, D0, T0, T0 - timedelta(days=1))
    out1 = Ledger(orders=(*rows, later)).known(T0).orders
    out2 = Ledger(orders=(later, *reversed(rows))).known(T0).orders
    assert out1 == out2
    assert [o.source_ref for o in out1] == ["z", "a", "b", "c"]


def test_application_picks_latest():
    assert Ledger().application is None
    old = Application("a1", Decimal(1), (), (), T0)
    new = Application("a2", Decimal(2), (), (), T0 + timedelta(days=1))
    assert Ledger(applications=(new, old)).application == new


def test_bank_txn_signed():
    assert BankTxn("t", "a", Decimal(5), "in", None, None, D, T0).signed == Decimal(5)
    assert BankTxn("t", "a", Decimal(5), "out", None, None, D, T0).signed == Decimal(-5)
