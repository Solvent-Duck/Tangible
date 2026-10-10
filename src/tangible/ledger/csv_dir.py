"""Read a synthetic-format output directory (one CSV per ledger table) into a `Ledger`.

Pure: no database. The directory layout is what `tangible.datasets.synthetic` writes: one file
per table with its body columns, then observed_at, evidence_pull_id, evidence_locator. Orders and
refunds are not in the directory; they come from the order-lines CSV via `uci_retail`.

Rows are returned with every revision (time filtering is `Ledger.known`'s job).
"""

import csv
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from tangible.datasets.uci_retail import build_ledger
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import (
    AccountBalance,
    BankTxn,
    InventorySnapshot,
    Invoice,
    Ledger,
    Payout,
)


def _rows(path: Path) -> Iterator[dict[str, str]]:
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def _opt_date(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def _ev(r: dict[str, str]) -> EvidenceRef:
    return EvidenceRef(r["evidence_pull_id"], r["evidence_locator"])


def _obs(r: dict[str, str]) -> datetime:
    return datetime.fromisoformat(r["observed_at"])


def read_ledger_dir(
    directory: Path,
    lines_csv: Path | None = None,
    observed_lag: timedelta = timedelta(0),
) -> Ledger:
    """Ledger from `directory`, plus orders/refunds from `lines_csv` when given."""
    base = build_ledger(lines_csv, observed_lag) if lines_csv else Ledger()
    payouts = tuple(
        Payout(
            r["source_ref"], r["processor"], Decimal(r["amount"]), Decimal(r["fees"]),
            _opt_date(r["expected_on"]), _opt_date(r["settled_on"]), _obs(r), _ev(r),
        )
        for r in _rows(directory / "payout.csv")
    )  # fmt: skip
    bank = tuple(
        BankTxn(
            r["source_ref"], r["account_ref"], Decimal(r["amount"]),
            cast(Literal["in", "out"], r["direction"]), r["counterparty"] or None,
            r["description"] or None, date.fromisoformat(r["posted_on"]), _obs(r), _ev(r),
        )
        for r in _rows(directory / "bank_txn.csv")
    )  # fmt: skip
    invoices = tuple(
        Invoice(
            r["source_ref"], cast(Literal["payable", "receivable"], r["kind"]),
            r["counterparty"] or None, Decimal(r["amount"]), date.fromisoformat(r["issued_on"]),
            _opt_date(r["due_on"]), _opt_date(r["paid_on"]), _obs(r), _ev(r),
        )
        for r in _rows(directory / "invoice.csv")
    )  # fmt: skip
    stock = tuple(
        InventorySnapshot(
            r["source_ref"], r["sku"], Decimal(r["units_on_hand"]),
            Decimal(r["unit_cost"]) if r["unit_cost"] else None,
            date.fromisoformat(r["as_of"]), _obs(r), _ev(r),
        )
        for r in _rows(directory / "inventory_snapshot.csv")
    )  # fmt: skip
    balances = tuple(
        AccountBalance(
            r["source_ref"], r["account_ref"], Decimal(r["balance"]),
            date.fromisoformat(r["as_of"]), _obs(r), _ev(r),
        )
        for r in _rows(directory / "account_balance.csv")
    )  # fmt: skip
    return Ledger(
        orders=base.orders,
        refunds=base.refunds,
        payouts=payouts,
        bank_txns=bank,
        invoices=invoices,
        inventory_snapshots=stock,
        account_balances=balances,
    )
