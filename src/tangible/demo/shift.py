"""Shift the UCI order lines forward in time by a whole number of weeks.

The UCI data ends in December 2011. A demo wants data that ends days before the demo, so the
dates move by a multiple of 7 days: weekdays keep their pattern (weekends stay weekends, so
payout and settlement timing look the same). Only the date part of `invoice_date` changes; the
time of day and the text format are preserved byte for byte.

Generating on the shifted lines (not shifting generated output) means every derived table
(payouts, bank, invoices, inventory, balances, observed_at) follows the same calendar.
"""

import csv
from datetime import date, timedelta
from pathlib import Path

from tangible.datasets.uci_retail import CSV_COLUMNS

DATE_COLUMN = "invoice_date"  # the only date/timestamp column in CSV_COLUMNS
DATE_CHARS = 10  # "YYYY-MM-DD" prefix of an ISO timestamp


def default_end_on(today: date | None = None) -> date:
    """A week before today, so the newest data is recent but not "today"."""
    return (today or date.today()) - timedelta(days=7)


def _date_of(stamp: str) -> date:
    return date.fromisoformat(stamp[:DATE_CHARS])


def shift_lines(src: Path, dst: Path, *, end_on: date) -> int:
    """Write a copy of the order-lines CSV with every date moved by a whole number of weeks so
    the latest order lands on or just before `end_on` (within the 7 days before it).

    Returns the offset in days (a multiple of 7; negative if the data must move back).
    """
    assert DATE_COLUMN in CSV_COLUMNS, "order-lines schema changed; update demo.shift"
    latest: date | None = None
    with src.open(newline="") as f:
        for record in csv.DictReader(f):
            d = _date_of(record[DATE_COLUMN])
            if latest is None or d > latest:
                latest = d
    if latest is None:
        raise ValueError(f"no order lines in {src}")
    offset = ((end_on - latest).days // 7) * 7  # floor: latest + offset <= end_on
    delta = timedelta(days=offset)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open(newline="") as f_in, dst.open("w", newline="") as f_out:
        reader = csv.reader(f_in)
        writer = csv.writer(f_out, lineterminator="\n")
        header = next(reader)
        writer.writerow(header)
        col = header.index(DATE_COLUMN)
        for row in reader:
            stamp = row[col]
            row[col] = (_date_of(stamp) + delta).isoformat() + stamp[DATE_CHARS:]
            writer.writerow(row)
    return offset
