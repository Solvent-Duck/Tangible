import csv
import hashlib
from pathlib import Path

import pytest

from tangible.datasets.synthetic import generate
from tangible.db.repo import load_ledger
from tangible.db.session import tenant_session
from tangible.documents.upload import store_upload
from tangible.evidence.resolve import EvidenceIntegrityError, resolve_line
from tangible.evidence.store import LocalEvidenceStore
from tangible.ledger.evidence import EvidenceRef
from test_ingest import ingest, setup_borrower
from test_synthetic import write_lines

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("resolve")
    lines = write_lines(root / "lines.csv", days=60)
    generate(lines, root / "out", seed=3)
    return root / "out", lines


def test_every_synthetic_payload_hashes_to_its_sha(
    test_db_url: str, data: tuple[Path, Path], tmp_path: Path
) -> None:
    tenant, borrower = setup_borrower(test_db_url, "payloads")
    ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    store = LocalEvidenceStore(tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        pulls = conn.execute(
            "SELECT id, source, s3_key, sha256, byte_size FROM evidence_pull"
        ).fetchall()
    assert len(pulls) > 2
    for pid, source, key, sha, size in pulls:
        blob = store.get(key)
        assert hashlib.sha256(blob).hexdigest() == sha and len(blob) == size, key
        if source != "orders_csv":
            assert key == f"{tenant}/{borrower}/pulls/{pid}.txt"


def test_resolve_returns_exact_row_line(
    test_db_url: str, data: tuple[Path, Path], tmp_path: Path
) -> None:
    tenant, borrower = setup_borrower(test_db_url, "exact")
    ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    store = LocalEvidenceStore(tmp_path / "s")
    with open(data[0] / "bank_txn.csv", newline="") as f:
        by_ref = {r["source_ref"]: r for r in csv.DictReader(f)}
    with tenant_session(test_db_url, tenant) as conn:
        ledger = load_ledger(conn, borrower)
        assert ledger.bank_txns
        for txn in (ledger.bank_txns[0], ledger.bank_txns[len(ledger.bank_txns) // 2],
                    ledger.bank_txns[-1]):  # fmt: skip
            assert txn.evidence is not None
            src = by_ref[txn.source_ref]
            body = [src[c] for c in list(src)[:-3]]
            expected = "|".join(["bank_txn", *body, src["observed_at"]])
            assert resolve_line(conn, store, txn.evidence) == expected
        # past the end, nonsense locators and unknown pulls are None
        pull = ledger.bank_txns[0].evidence.pull_id  # type: ignore[union-attr]
        assert resolve_line(conn, store, EvidenceRef(pull, "row=999999")) is None
        assert resolve_line(conn, store, EvidenceRef(pull, "row=0")) is None
        assert resolve_line(conn, store, EvidenceRef(pull, "bogus")) is None
        assert (
            resolve_line(conn, store, EvidenceRef("00000000-0000-0000-0000-000000000000", "row=1"))
            is None
        )


def test_tampered_payload_raises(test_db_url: str, data: tuple[Path, Path], tmp_path: Path) -> None:
    tenant, borrower = setup_borrower(test_db_url, "tamper")
    ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    store = LocalEvidenceStore(tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        txn = load_ledger(conn, borrower).bank_txns[0]
        assert txn.evidence is not None
        assert resolve_line(conn, store, txn.evidence)
        (key,) = conn.execute(
            "SELECT s3_key FROM evidence_pull WHERE id = %s", (txn.evidence.pull_id,)
        ).fetchone()
        path = tmp_path / "s" / key
        path.write_bytes(path.read_bytes().replace(b"bank_txn", b"bank_txm", 1))
        with pytest.raises(EvidenceIntegrityError):
            resolve_line(conn, store, txn.evidence)


def test_ingest_refuses_unverifiable_pull(
    test_db_url: str, data: tuple[Path, Path], tmp_path: Path
) -> None:
    import shutil

    bad = tmp_path / "bad"
    shutil.copytree(data[0], bad)
    text = (bad / "bank_txn.csv").read_text()
    (bad / "bank_txn.csv").write_text(text.replace("bank", "bonk", 1) if "bank" in text else text)
    # change an amount instead: guaranteed to alter a payload line
    rows = list(csv.reader((bad / "bank_txn.csv").open(newline="")))
    amt = rows[0].index("amount")
    rows[1][amt] = "999999.99"
    with (bad / "bank_txn.csv").open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    tenant, borrower = setup_borrower(test_db_url, "unverifiable")
    with pytest.raises(ValueError, match="sha256"):
        ingest(test_db_url, tenant, borrower, (bad, data[1]), tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        assert conn.execute("SELECT count(*) FROM evidence_pull").fetchone() == (0,)


def test_invoice_and_statement_locators(
    test_db_url: str, data: tuple[Path, Path], tmp_path: Path
) -> None:
    tenant, borrower = setup_borrower(test_db_url, "locators")
    ingest(test_db_url, tenant, borrower, data, tmp_path / "s")
    store = LocalEvidenceStore(tmp_path / "s")
    with tenant_session(test_db_url, tenant) as conn:
        order = load_ledger(conn, borrower).orders[0]
        assert order.evidence is not None and order.evidence.locator.startswith("invoice=")
        got = resolve_line(conn, store, order.evidence)
        assert got is not None
        expected = [
            ln for ln in data[1].read_text().splitlines()[1:] if f",{order.source_ref}," in ln
        ]
        assert got.splitlines() == expected
        pid = store_upload(conn, store, tenant, borrower, "Jan statement.pdf", b"%PDF-1.4 fake")
        desc = resolve_line(conn, store, EvidenceRef(pid, "statement"))
        assert desc == "uploaded document Jan_statement.pdf, 13 bytes"
        assert resolve_line(conn, store, EvidenceRef(pid, "row=1")) is not None  # raw line 1
