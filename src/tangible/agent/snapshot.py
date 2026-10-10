"""Frozen SQLite snapshot of a time-filtered ledger, for `query_ledger` and `run_analysis`.

Money stays exact: every Decimal column is stored as TEXT (the exact value) plus an INTEGER
`<col>_minor` companion (scale 2; `unit_cost` uses `unit_cost_micro`, scale 4) so SUMs are exact.
Same input gives a byte-identical file: fixed insert order, no timestamps, journal off, vacuumed.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from tangible.ledger.types import Ledger

TABLES = (
    "orders",
    "refunds",
    "payouts",
    "bank_txns",
    "invoices",
    "inventory_snapshots",
    "account_balances",
)

# (column, kind) per table, in storage order. kinds: text, dec (scale 2), dec4 (scale 4).
_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (
        ("source_ref", "text"), ("gross", "dec"), ("discounts", "dec"),
        ("occurred_at", "text"), ("observed_at", "text"), ("customer_ref", "text"),
        ("channel", "text"), ("currency", "text"),
    ),
    "refunds": (
        ("source_ref", "text"), ("order_ref", "text"), ("amount", "dec"),
        ("occurred_at", "text"), ("observed_at", "text"),
    ),
    "payouts": (
        ("source_ref", "text"), ("processor", "text"), ("amount", "dec"), ("fees", "dec"),
        ("expected_on", "text"), ("settled_on", "text"), ("observed_at", "text"),
    ),
    "bank_txns": (
        ("source_ref", "text"), ("account_ref", "text"), ("amount", "dec"),
        ("direction", "text"), ("counterparty", "text"), ("description", "text"),
        ("posted_on", "text"), ("observed_at", "text"),
    ),
    "invoices": (
        ("source_ref", "text"), ("kind", "text"), ("counterparty", "text"), ("amount", "dec"),
        ("issued_on", "text"), ("due_on", "text"), ("paid_on", "text"), ("observed_at", "text"),
    ),
    "inventory_snapshots": (
        ("source_ref", "text"), ("sku", "text"), ("units_on_hand", "dec"),
        ("unit_cost", "dec4"), ("as_of", "text"), ("observed_at", "text"),
    ),
    "account_balances": (
        ("source_ref", "text"), ("account_ref", "text"), ("balance", "dec"),
        ("as_of", "text"), ("observed_at", "text"),
    ),
}  # fmt: skip

_DESCRIPTIONS = {
    "orders": "Sales orders. gross and discounts are in the order currency.",
    "refunds": "Refunds against orders (order_ref = orders.source_ref).",
    "payouts": "Payment-processor payouts with fees.",
    "bank_txns": "Bank transactions. amount is always positive; direction is 'in' or 'out'.",
    "invoices": "Payable and receivable invoices.",
    "inventory_snapshots": "Inventory counts per SKU at a snapshot date.",
    "account_balances": "Bank account balances at a date.",
}

_SCALE = {"dec": 2, "dec4": 4}


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    sha256: str
    row_counts: dict[str, int]


def minor_name(col: str, kind: str) -> str:
    return f"{col}_micro" if kind == "dec4" else f"{col}_minor"


def _minor(d: Decimal, scale: int) -> int:
    return int((d * (10**scale)).to_integral_value(rounding=ROUND_HALF_EVEN))


def _cell(value: Any, kind: str) -> list[Any]:
    if kind == "text":
        if value is None:
            return [None]
        if isinstance(value, datetime | date):
            return [value.isoformat()]
        return [str(value)]
    if value is None:
        return [None, None]
    return [str(value), _minor(value, _SCALE[kind])]


def _ddl(table: str) -> str:
    cols: list[str] = []
    for name, kind in _COLUMNS[table]:
        cols.append(f"{name} TEXT")
        if kind != "text":
            cols.append(f"{minor_name(name, kind)} INTEGER")
    cols.append("evidence TEXT")
    return f"CREATE TABLE {table} ({', '.join(cols)})"


def schema_summary() -> dict[str, dict[str, Any]]:
    """Each snapshot table's purpose and columns in storage order, as query_ledger sees them."""
    out: dict[str, dict[str, Any]] = {}
    for table in TABLES:
        cols: list[str] = []
        for name, kind in _COLUMNS[table]:
            cols.append(name)
            if kind != "text":
                cols.append(minor_name(name, kind))
        out[table] = {"about": _DESCRIPTIONS[table], "columns": [*cols, "evidence"]}
    return out


def build_snapshot(known: Ledger, path: Path, as_of: datetime | None = None) -> SnapshotInfo:
    """Write `known` (already time-filtered by the caller) to a SQLite file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA journal_mode=OFF")
        counts: dict[str, int] = {}
        for table in TABLES:
            con.execute(_ddl(table))
            rows = getattr(known, table)
            counts[table] = len(rows)
            width = sum(1 if k == "text" else 2 for _, k in _COLUMNS[table]) + 1
            stmt = f"INSERT INTO {table} VALUES ({', '.join('?' * width)})"
            for r in rows:
                vals: list[Any] = []
                for name, kind in _COLUMNS[table]:
                    vals.extend(_cell(getattr(r, name), kind))
                vals.append(str(r.evidence) if r.evidence is not None else None)
                con.execute(stmt, vals)
        columns = {
            t: {
                **{n: "text" for n, k in _COLUMNS[t] if k == "text"},
                **{
                    n: f"exact decimal as text; integer companion {minor_name(n, k)} "
                    f"= value x 10^{_SCALE[k]} (sum the integer column for exact totals)"
                    for n, k in _COLUMNS[t]
                    if k != "text"
                },
                "evidence": "pointer to the raw source row, '<pull_id>#<locator>'",
            }
            for t in TABLES
        }
        meta = {
            "as_of": as_of.isoformat() if as_of else "",
            "tables": json.dumps(_DESCRIPTIONS, sort_keys=True),
            "columns": json.dumps(columns, sort_keys=True),
            "units": "dates and datetimes are ISO-8601 text; observed_at is when the lender "
            "could first have known the row",
            "row_counts": json.dumps(counts, sort_keys=True),
        }
        con.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
        for k, v in meta.items():
            con.execute("INSERT INTO _meta VALUES (?, ?)", (k, v))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return SnapshotInfo(path, hashlib.sha256(path.read_bytes()).hexdigest(), counts)
