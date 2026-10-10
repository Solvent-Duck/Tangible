import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tangible.agent.snapshot import TABLES, build_snapshot
from tangible.ledger.types import Ledger

CENT_COLS = {
    "orders": ["gross", "discounts"],
    "refunds": ["amount"],
    "payouts": ["amount", "fees"],
    "bank_txns": ["amount"],
    "invoices": ["amount"],
    "inventory_snapshots": ["units_on_hand"],
    "account_balances": ["balance"],
}


@pytest.fixture(scope="module")
def known(synthetic_ledger: Any) -> Ledger:
    ledger: Ledger = synthetic_ledger(days=60)
    assert all(len(getattr(ledger, t)) > 0 for t in ("orders", "payouts", "bank_txns"))
    return ledger.known(datetime(2100, 1, 1, tzinfo=UTC))


def rows(path: Path, table: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}")]
    finally:
        con.close()


def test_rows_equal_known_exactly(known: Ledger, tmp_path: Path) -> None:
    info = build_snapshot(known, tmp_path / "s.sqlite")
    for t in TABLES:
        src = getattr(known, t)
        got = rows(info.path, t)
        assert info.row_counts[t] == len(src) == len(got), t
        for r, g in zip(src, got, strict=True):
            assert g["source_ref"] == r.source_ref
            assert g["observed_at"] == r.observed_at.isoformat()
            assert g["evidence"] == (str(r.evidence) if r.evidence else None)
            for c in CENT_COLS[t]:
                assert Decimal(g[c]) == getattr(r, c)
        for c in CENT_COLS[t]:
            total = sum((getattr(r, c) for r in src), Decimal(0))
            assert sum(g[f"{c}_minor"] for g in got) == total * 100, (t, c)
    inv = rows(info.path, "inventory_snapshots")
    for r, g in zip(known.inventory_snapshots, inv, strict=True):
        if r.unit_cost is None:
            assert g["unit_cost"] is None and g["unit_cost_micro"] is None
        else:
            assert Decimal(g["unit_cost"]) == r.unit_cost
            assert g["unit_cost_micro"] == r.unit_cost * 10000


def test_meta_present(known: Ledger, tmp_path: Path) -> None:
    as_of = datetime(2010, 1, 1, tzinfo=UTC)
    info = build_snapshot(known, tmp_path / "s.sqlite", as_of)
    meta = {r["key"]: r["value"] for r in rows(info.path, "_meta")}
    assert meta["as_of"] == as_of.isoformat()
    assert {"tables", "columns", "units"} <= set(meta)


def test_deterministic(known: Ledger, tmp_path: Path) -> None:
    a = build_snapshot(known, tmp_path / "a.sqlite")
    b = build_snapshot(known, tmp_path / "b.sqlite")
    again = build_snapshot(known, tmp_path / "a.sqlite")  # overwrites
    assert a.path.read_bytes() == b.path.read_bytes()
    assert a.sha256 == b.sha256 == again.sha256


def test_rows_observed_after_as_of_absent(known: Ledger, tmp_path: Path) -> None:
    last = max(r.observed_at for t in TABLES for r in getattr(known, t))
    first = min(r.observed_at for t in TABLES for r in getattr(known, t))
    cutoff = first - timedelta(seconds=1)
    early = build_snapshot(known.known(cutoff), tmp_path / "e.sqlite")
    assert all(n == 0 for n in early.row_counts.values())
    full = build_snapshot(known.known(last), tmp_path / "f.sqlite")
    assert full.row_counts["orders"] == len(known.orders)
