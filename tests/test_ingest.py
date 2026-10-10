import dataclasses
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tangible.datasets.synthetic import generate
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.ledger.csv_dir import read_ledger_dir
from tangible.ledger.types import Ledger
from test_synthetic import write_lines

pytestmark = pytest.mark.db

TABLES = [
    "orders", "refunds", "payouts", "bank_txns", "invoices", "inventory_snapshots",
    "account_balances",
]  # fmt: skip
CENT = Decimal("0.01")


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("ingest")
    lines = write_lines(root / "lines.csv", days=60)
    generate(lines, root / "out", seed=3)
    return root / "out", lines


def new_tenant(url: str, name: str) -> str:
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
        assert row
        return str(row[0])


def ingest(url: str, tenant: str, borrower: str, data: tuple[Path, Path], store: Path):
    with tenant_session(url, tenant) as conn:
        return ingest_synthetic_dir(
            conn, LocalEvidenceStore(store), tenant, borrower, data[0], data[1]
        )


def setup_borrower(url: str, name: str) -> tuple[str, str]:
    tenant = new_tenant(url, name)
    with tenant_session(url, tenant) as conn:
        return tenant, create_borrower(conn, tenant, name)


def stripped(rows: tuple[Any, ...]) -> list[Any]:
    """Rows without evidence; orders/refunds amounts quantized to the column's 2 decimals."""
    out = []
    for r in rows:
        changes: dict[str, Any] = {"evidence": None}
        for f in ("gross", "discounts", "amount"):
            if f in {x.name for x in dataclasses.fields(r)} and r.__class__.__name__ in (
                "Order",
                "Refund",
            ):
                changes[f] = getattr(r, f).quantize(CENT)
        out.append(dataclasses.replace(r, **changes))
    return sorted(out, key=repr)


def locators(rows: tuple[Any, ...]) -> list[tuple[str, str]]:
    assert all(r.evidence is not None for r in rows)
    return sorted((r.source_ref, r.evidence.locator) for r in rows)


def test_round_trip(test_db_url: str, data: tuple[Path, Path], tmp_path: Path):
    tenant, borrower = setup_borrower(test_db_url, "rt")
    res = ingest(test_db_url, tenant, borrower, data, tmp_path / "store")
    assert res.pulls_inserted > 1 and res.pulls_skipped == 0
    expected = read_ledger_dir(*data)
    with tenant_session(test_db_url, tenant) as conn:
        loaded = load_ledger(conn, borrower)
    for t in TABLES:
        exp, got = getattr(expected, t), getattr(loaded, t)
        assert len(exp) > 0 or t == "refunds" or t == "inventory_snapshots", t
        assert stripped(got) == stripped(exp), t
        assert all(r.evidence for r in got), t  # every loaded row has evidence
        if t not in ("orders", "refunds"):  # UCI rows carry no evidence until ingest
            assert locators(got) == locators(exp), t
        assert res.rows_by_table.get(
            {"refunds": "refund", "bank_txns": "bank_txn", "payouts": "payout",
             "invoices": "invoice", "inventory_snapshots": "inventory_snapshot",
             "account_balances": "account_balance", "orders": "orders"}[t], 0
        ) == len(exp)  # fmt: skip
    assert loaded.connections == () and loaded.applications == ()
    # orders carry the lines-file pull and an invoice= locator
    assert all(
        o.evidence and o.evidence.locator == f"invoice={o.source_ref}" for o in loaded.orders
    )
    assert len({o.evidence.pull_id for o in loaded.orders if o.evidence}) == 1
    # deterministic order
    for t in TABLES:
        rows = getattr(loaded, t)
        keys = [(r.observed_at, r.source_ref) for r in rows]
        assert keys == sorted(keys)
    # raw files landed in the store
    assert (tmp_path / "store" / tenant / borrower / "payout.csv").exists()
    assert (tmp_path / "store" / tenant / borrower / "lines.csv").exists()


def test_reingest_is_noop(test_db_url: str, data: tuple[Path, Path], tmp_path: Path):
    tenant, borrower = setup_borrower(test_db_url, "again")
    first = ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        before = load_ledger(conn, borrower)
    second = ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    assert second.pulls_inserted == 0
    assert second.pulls_skipped == first.pulls_inserted
    assert second.rows_by_table == {} or all(v == 0 for v in second.rows_by_table.values())
    with tenant_session(test_db_url, tenant) as conn:
        after = load_ledger(conn, borrower)
    for t in TABLES:
        assert len(getattr(after, t)) == len(getattr(before, t)), t


def test_two_borrowers_same_data(test_db_url: str, data: tuple[Path, Path], tmp_path: Path):
    tenant = new_tenant(test_db_url, "two")
    with tenant_session(test_db_url, tenant) as conn:
        b1 = create_borrower(conn, tenant, "one")
        b2 = create_borrower(conn, tenant, "two")
    r1 = ingest(test_db_url, tenant, b1, data, tmp_path / "s")
    r2 = ingest(test_db_url, tenant, b2, data, tmp_path / "s")
    assert r1.pulls_inserted == r2.pulls_inserted > 0
    with tenant_session(test_db_url, tenant) as conn:
        l1, l2 = load_ledger(conn, b1), load_ledger(conn, b2)
    assert stripped(l1.bank_txns) == stripped(l2.bank_txns)
    assert {r.evidence.pull_id for r in l1.bank_txns if r.evidence}.isdisjoint(
        {r.evidence.pull_id for r in l2.bank_txns if r.evidence}
    )


def test_rls_hides_other_tenants_ledger(test_db_url: str, data: tuple[Path, Path], tmp_path: Path):
    ta, ba = setup_borrower(test_db_url, "rls-a")
    tb, _ = setup_borrower(test_db_url, "rls-b")
    ingest(test_db_url, ta, ba, data, tmp_path / "s")
    with tenant_session(test_db_url, tb) as conn:
        for table in ("orders", "payout", "bank_txn", "evidence_pull"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            assert row == (0,), table
        empty = load_ledger(conn, ba)
    assert empty == Ledger()
    with tenant_session(test_db_url, ta) as conn:
        assert load_ledger(conn, ba).orders


def test_known_drops_later_observed(test_db_url: str, data: tuple[Path, Path], tmp_path: Path):
    tenant, borrower = setup_borrower(test_db_url, "known")
    ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        full = load_ledger(conn, borrower)
    cut = datetime(2010, 2, 15, tzinfo=UTC)
    known = full.known(cut)
    assert 0 < len(known.orders) < len(full.orders)
    assert all(o.observed_at <= cut for o in known.orders)
    assert all(b.observed_at <= cut for b in known.bank_txns)


REAL_DIR = Path("data/synthetic")
REAL_LINES = Path("data/processed/uci_lines.csv")


@pytest.mark.skipif(
    not (REAL_DIR.exists() and REAL_LINES.exists()), reason="real dataset not present"
)
def test_real_ingest_timing(test_db_url: str, tmp_path: Path):
    tenant, borrower = setup_borrower(test_db_url, "real")
    t0 = time.monotonic()
    res = ingest(test_db_url, tenant, borrower, (REAL_DIR, REAL_LINES), tmp_path / "s")
    elapsed = time.monotonic() - t0
    print(f"real ingest: {elapsed:.1f}s {res}")
    assert res.rows_by_table["orders"] > 1000
    assert elapsed < 60
