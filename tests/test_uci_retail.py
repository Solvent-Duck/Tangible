from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tangible.datasets.uci_retail import CSV_COLUMNS, build_ledger

REAL = Path(__file__).parent.parent / "data/processed/uci_lines.csv"


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    path.write_text("\n".join([",".join(CSV_COLUMNS), *(",".join(r) for r in rows)]) + "\n")
    return path


def test_invoices_become_orders_credit_notes_become_refunds(tmp_path: Path):
    #        sheet inv     code   desc qty  date                 price cust   country
    rows = [
        ["0", "1001", "A1", "x", "2", "2010-01-04T09:00:00", "5.00", "42", "UK"],
        ["0", "1001", "D", "d", "-1", "2010-01-04T09:00:00", "3.00", "42", "UK"],
        ["0", "1001", "BANK CHARGES", "f", "1", "2010-01-04T09:00:00", "9.00", "", "UK"],
        ["0", "1002", "A1", "x", "1", "2010-01-05T09:00:00", "0", "", "UK"],  # price 0: dropped
        ["0", "C1003", "A1", "x", "-3", "2010-01-06T09:00:00", "5.00", "42", "UK"],
        ["0", "A1004", "B", "bad debt", "1", "2010-01-06T09:00:00", "-50", "", "UK"],
    ]
    ledger = build_ledger(write_csv(tmp_path / "l.csv", rows), observed_lag=timedelta(days=1))
    (order,) = ledger.orders
    assert (order.source_ref, order.gross, order.discounts) == ("1001", Decimal(10), Decimal(3))
    assert order.observed_at - order.occurred_at == timedelta(days=1)
    (refund,) = ledger.refunds
    assert (refund.source_ref, refund.amount) == ("C1003", Decimal(15))


def test_sheet_overlap_takes_invoice_from_first_sheet_only(tmp_path: Path):
    rows = [
        ["0", "2001", "A1", "x", "1", "2010-12-02T09:00:00", "10", "7", "UK"],
        ["1", "2001", "A1", "x", "1", "2010-12-02T09:00:00", "10", "7", "UK"],
    ]
    (order,) = build_ledger(write_csv(tmp_path / "l.csv", rows)).orders
    assert order.gross == Decimal(10)


@pytest.mark.skipif(not REAL.exists(), reason="run the xlsx_to_csv step first")
def test_real_data_shape():
    ledger = build_ledger(REAL)
    assert 39_000 < len(ledger.orders) < 41_000
    assert 7_500 < len(ledger.refunds) < 8_500
