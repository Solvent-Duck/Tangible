from datetime import UTC, datetime

from tangible.ledger.evidence import EvidenceRef


def test_synthetic_ledger_round_trips_every_table(synthetic_ledger):
    ledger = synthetic_ledger()
    for name in ("orders", "refunds", "payouts", "bank_txns", "invoices",
                 "inventory_snapshots", "account_balances"):  # fmt: skip
        assert getattr(ledger, name), name
    ref = ledger.bank_txns[0].evidence
    assert isinstance(ref, EvidenceRef) and ref.locator.startswith("row=")
    assert all(t.amount > 0 for t in ledger.bank_txns)


def test_invoice_revisions_resolve_through_known(synthetic_ledger):
    ledger = synthetic_ledger()
    paid = next(i for i in ledger.invoices if i.paid_on)
    issued = next(i for i in ledger.invoices if i.source_ref == paid.source_ref and not i.paid_on)
    before = ledger.known(issued.observed_at)
    after = ledger.known(paid.observed_at)
    assert next(i for i in before.invoices if i.source_ref == paid.source_ref).paid_on is None
    assert next(i for i in after.invoices if i.source_ref == paid.source_ref).paid_on


def test_plant_changes_bank_rows(synthetic_ledger):
    clean = synthetic_ledger()
    debit = synthetic_ledger(plant={"undisclosed_lender_debit"})
    assert not [t for t in clean.bank_txns if t.counterparty == "YRDEAL CAPITAL"]
    assert [t for t in debit.bank_txns if t.counterparty == "YRDEAL CAPITAL"]
    assert datetime(2030, 1, 1, tzinfo=UTC) > max(t.observed_at for t in debit.bank_txns)
