"""Demo date shift: whole weeks, latest order on or just before end_on, format preserved."""

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from tangible.datasets.synthetic import generate
from tangible.datasets.uci_retail import read_lines
from tangible.demo.shift import default_end_on, shift_lines
from tangible.ledger.csv_dir import read_ledger_dir
from test_synthetic import write_lines


def stamps(path: Path) -> list[str]:
    with path.open(newline="") as f:
        return [r["invoice_date"] for r in csv.DictReader(f)]


@pytest.mark.parametrize(
    "end_on",
    [
        date(2026, 10, 3),  # a Saturday
        date(2026, 10, 9),
        date(2026, 10, 10),
        date(2010, 7, 1),  # inside the data: moves it back
        date(2030, 2, 28),
    ],
)
def test_shift_is_whole_weeks_and_lands_just_before_end_on(tmp_path: Path, end_on: date) -> None:
    src = write_lines(tmp_path / "lines.csv", days=60)
    dst = tmp_path / "shifted.csv"
    offset = shift_lines(src, dst, end_on=end_on)
    assert offset % 7 == 0

    before, after = stamps(src), stamps(dst)
    assert len(before) == len(after) > 0
    latest = max(date.fromisoformat(s[:10]) for s in after)
    assert end_on - timedelta(days=7) < latest <= end_on
    for b, a in zip(before, after, strict=True):
        assert (date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days == offset
        assert a[10:] == b[10:]  # time of day and format untouched
        assert len(a) == len(b)
        assert date.fromisoformat(a[:10]).weekday() == date.fromisoformat(b[:10]).weekday()


def test_shift_changes_only_the_date_column(tmp_path: Path) -> None:
    src = write_lines(tmp_path / "lines.csv", days=30)
    dst = tmp_path / "shifted.csv"
    shift_lines(src, dst, end_on=date(2026, 10, 3))
    with src.open(newline="") as a, dst.open(newline="") as b:
        for ra, rb in zip(csv.DictReader(a), csv.DictReader(b), strict=True):
            ra.pop("invoice_date")
            rb.pop("invoice_date")
            assert ra == rb
    assert src.read_text().splitlines()[0] == dst.read_text().splitlines()[0]


def test_shifted_lines_still_load(tmp_path: Path) -> None:
    src = write_lines(tmp_path / "lines.csv", days=30)
    dst = tmp_path / "shifted.csv"
    shift_lines(src, dst, end_on=date(2026, 10, 3))
    old, new = list(read_lines(src)), list(read_lines(dst))
    assert [(x.invoice, x.price, x.quantity) for x in old] == [
        (x.invoice, x.price, x.quantity) for x in new
    ]
    assert max(x.occurred_at for x in new).date() <= date(2026, 10, 3)


def test_empty_lines_file_is_an_error(tmp_path: Path) -> None:
    src = tmp_path / "empty.csv"
    src.write_text("sheet,invoice,stock_code,description,quantity,invoice_date,price,"
                   "customer_id,country\n")  # fmt: skip
    with pytest.raises(ValueError, match="no order lines"):
        shift_lines(src, tmp_path / "out.csv", end_on=date(2026, 10, 3))


def test_default_end_on_is_a_week_before_today() -> None:
    assert default_end_on(date(2026, 10, 10)) == date(2026, 10, 3)


def test_generated_tables_follow_the_shifted_calendar(tmp_path: Path) -> None:
    src = write_lines(tmp_path / "lines.csv", days=200)
    dst = tmp_path / "shifted.csv"
    end_on = date(2026, 10, 3)
    shift_lines(src, dst, end_on=end_on)
    generate(dst, tmp_path / "gen", seed=7)
    ledger = read_ledger_dir(tmp_path / "gen", dst)

    days = [o.occurred_at.date() for o in ledger.orders]
    first, last = min(days), max(days)
    assert end_on - timedelta(days=7) < last <= end_on
    assert end_on - timedelta(days=260) < first  # 200 days of orders, moved to 2026

    def inside(d: date) -> bool:
        return first <= d <= last

    assert ledger.payouts and ledger.bank_txns and ledger.invoices
    assert all(inside(p.settled_on) for p in ledger.payouts if p.settled_on)
    assert all(inside(t.posted_on) for t in ledger.bank_txns)
    assert all(inside(i.issued_on) for i in ledger.invoices)
    assert all(inside(s.as_of) for s in ledger.inventory_snapshots)
    assert all(inside(b.as_of) for b in ledger.account_balances)
    for rows in (ledger.payouts, ledger.bank_txns, ledger.invoices, ledger.account_balances):
        assert all(first <= r.observed_at.date() <= last + timedelta(days=2) for r in rows)
