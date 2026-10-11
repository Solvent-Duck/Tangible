"""Ingest a synthetic-format directory plus the UCI order-lines file into the database.

Evidence pull ids in the synthetic CSVs are deterministic and identical across borrowers, so each
is remapped to uuid5(borrower_id, original_id); that lets several borrowers hold the same data.
Idempotent: a pull whose (borrower_id, sha256) already exists is skipped along with its rows.
"""

import csv
import hashlib
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tangible.datasets.uci_retail import build_ledger
from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.ledger.csv_dir import read_ledger_dir
from tangible.ledger.evidence import EvidenceRef

Values = tuple[Any, ...]


@dataclass
class IngestResult:
    pulls_inserted: int = 0
    pulls_skipped: int = 0
    rows_by_table: dict[str, int] = field(default_factory=dict)


# table -> (body columns, row -> values for body columns)
_TABLES: dict[str, tuple[tuple[str, ...], Callable[[Any], Values]]] = {
    "payout": (
        ("source_ref", "processor", "amount", "fees", "expected_on", "settled_on", "observed_at"),
        lambda r: (r.source_ref, r.processor, r.amount, r.fees, r.expected_on, r.settled_on,
                   r.observed_at),
    ),
    "bank_txn": (
        ("source_ref", "account_ref", "amount", "direction", "counterparty", "description",
         "posted_on", "observed_at"),
        lambda r: (r.source_ref, r.account_ref, r.amount, r.direction, r.counterparty,
                   r.description, r.posted_on, r.observed_at),
    ),
    "invoice": (
        ("source_ref", "kind", "counterparty", "amount", "issued_on", "due_on", "paid_on",
         "observed_at"),
        lambda r: (r.source_ref, r.kind, r.counterparty, r.amount, r.issued_on, r.due_on,
                   r.paid_on, r.observed_at),
    ),
    "inventory_snapshot": (
        ("source_ref", "sku", "units_on_hand", "unit_cost", "as_of", "observed_at"),
        lambda r: (r.source_ref, r.sku, r.units_on_hand, r.unit_cost, r.as_of, r.observed_at),
    ),
    "account_balance": (
        ("source_ref", "account_ref", "balance", "as_of", "observed_at"),
        lambda r: (r.source_ref, r.account_ref, r.balance, r.as_of, r.observed_at),
    ),
    "orders": (
        ("source_ref", "channel", "gross", "discounts", "tax", "currency", "customer_ref",
         "occurred_at", "observed_at"),
        lambda r: (r.source_ref, None, r.gross, r.discounts, 0, "USD", r.customer_ref,
                   r.occurred_at, r.observed_at),
    ),
    "refund": (
        ("source_ref", "order_ref", "amount", "occurred_at", "observed_at"),
        lambda r: (r.source_ref, r.order_ref, r.amount, r.occurred_at, r.observed_at),
    ),
}  # fmt: skip

_PULL_COLS = ("id", "tenant_id", "borrower_id", "source", "fetched_at", "s3_key", "sha256",
              "byte_size", "status")  # fmt: skip
_EV_COLS = ("tenant_id", "borrower_id", "evidence_pull_id", "evidence_locator")


def _copy(conn: Conn, table: str, cols: tuple[str, ...], rows: Iterable[Values]) -> int:
    """Bulk insert. (COPY is refused under row-level security, so batched executemany.)"""
    query = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    n = 0
    batch: list[Values] = []
    with conn.cursor() as cur:
        for row in rows:
            batch.append(row)
            if len(batch) >= 5000:
                cur.executemany(query, batch)
                n += len(batch)
                batch = []
        if batch:
            cur.executemany(query, batch)
            n += len(batch)
    return n


def _pull_exists(conn: Conn, borrower_id: str, sha: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence_pull WHERE borrower_id = %s AND sha256 = %s LIMIT 1",
            (borrower_id, sha),
        ).fetchone()
        is not None
    )


_TRAILING = ("observed_at", "evidence_pull_id", "evidence_locator")


def _synthetic_payloads(directory: Path) -> dict[str, bytes]:
    """Rebuild each synthetic pull's raw payload, keyed by the CSVs' original pull id.

    The generator never writes payloads to disk; it hashes `table|body...|observed_at` lines, one
    per ledger row, ordered by (table, observed_at, body) within the pull. Reimplemented here
    (not imported) from the table CSVs. Each row's locator must equal its rebuilt line number.
    """
    by_pull: dict[str, list[tuple[str, datetime, tuple[str, ...], str, str]]] = {}
    for path in sorted(directory.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            if tuple(header[-3:]) != _TRAILING:
                continue  # not a ledger table (e.g. evidence_pull.csv)
            for r in reader:
                body = tuple(r[:-3])
                by_pull.setdefault(r[-2], []).append(
                    (path.stem, datetime.fromisoformat(r[-3]), body, r[-3], r[-1])
                )
    out: dict[str, bytes] = {}
    for pull_id, entries in by_pull.items():
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        lines: list[str] = []
        for n, (table, _, body, observed, locator) in enumerate(entries, 1):
            if locator != f"row={n}":
                raise ValueError(f"pull {pull_id}: {table} row locator {locator!r} != row={n}")
            lines.append("|".join((table, *body, observed)))
        out[pull_id] = ("\n".join(lines) + "\n").encode()
    return out


def ingest_synthetic_dir(
    conn: Conn,
    store: EvidenceStore,
    tenant_id: str,
    borrower_id: str,
    directory: Path,
    lines_csv: Path,
) -> IngestResult:
    tenant_id, borrower_id = str(tenant_id), str(borrower_id)
    ns = uuid.UUID(borrower_id)
    result = IngestResult()
    counts: Counter[str] = Counter()
    prefix = f"{tenant_id}/{borrower_id}"

    # 1. raw files into the write-once store
    for path in sorted(directory.glob("*.csv")):
        store.put(f"{prefix}/{path.name}", path.read_bytes())
    lines_bytes = lines_csv.read_bytes()
    lines_key = f"{prefix}/{lines_csv.name}"
    lines_sha = store.put(lines_key, lines_bytes)

    with conn.transaction():
        # 2. synthetic pulls
        remap: dict[str, str] = {}
        skipped: set[str] = set()
        payloads = _synthetic_payloads(directory)
        pull_rows: list[Values] = []
        with (directory / "evidence_pull.csv").open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                orig = r["id"]
                remap[orig] = str(uuid.uuid5(ns, orig))
                if _pull_exists(conn, borrower_id, r["sha256"]):
                    skipped.add(orig)
                    result.pulls_skipped += 1
                    continue
                payload = payloads.get(orig, b"")
                if hashlib.sha256(payload).hexdigest() != r["sha256"] or len(payload) != int(
                    r["byte_size"]
                ):
                    raise ValueError(
                        f"synthetic pull {orig}: rebuilt payload does not match the recorded "
                        "sha256; refusing to store unverifiable evidence"
                    )
                key = f"{prefix}/pulls/{remap[orig]}.txt"
                store.put(key, payload)
                pull_rows.append(
                    (remap[orig], tenant_id, borrower_id, r["source"],
                     datetime.fromisoformat(r["fetched_at"]), key, r["sha256"],
                     int(r["byte_size"]), r["status"])
                )  # fmt: skip
                result.pulls_inserted += 1

        # 3. order-lines pull
        orders_pull = str(uuid.uuid5(ns, f"orders_csv|{lines_sha}"))
        orders_skipped = _pull_exists(conn, borrower_id, lines_sha)
        if orders_skipped:
            result.pulls_skipped += 1
        else:
            pull_rows.append(
                (orders_pull, tenant_id, borrower_id, "orders_csv", datetime.now(UTC), lines_key,
                 lines_sha, len(lines_bytes), "complete")
            )  # fmt: skip
            result.pulls_inserted += 1
        _copy(conn, "evidence_pull", _PULL_COLS, pull_rows)

        # 4. ledger rows (pulls exist first: FK)
        def remapped(table: str, rows: Iterable[Any]) -> Iterator[Values]:
            to_values = _TABLES[table][1]
            for r in rows:
                ev: EvidenceRef | None = r.evidence
                assert ev is not None
                if ev.pull_id in skipped:
                    continue
                if ev.pull_id not in remap:
                    raise ValueError(f"{table} {r.source_ref}: unknown evidence pull {ev.pull_id}")
                yield (*to_values(r), tenant_id, borrower_id, remap[ev.pull_id], ev.locator)

        ledger = read_ledger_dir(directory)
        for table, rows in (
            ("payout", ledger.payouts),
            ("bank_txn", ledger.bank_txns),
            ("invoice", ledger.invoices),
            ("inventory_snapshot", ledger.inventory_snapshots),
            ("account_balance", ledger.account_balances),
        ):
            cols = (*_TABLES[table][0], *_EV_COLS)
            counts[table] += _copy(conn, table, cols, remapped(table, rows))

        if not orders_skipped:
            lines = build_ledger(lines_csv)
            order_rows: list[tuple[str, tuple[Any, ...]]] = [
                ("orders", lines.orders),
                ("refund", lines.refunds),
            ]
            for table, rows2 in order_rows:
                to_values = _TABLES[table][1]
                cols = (*_TABLES[table][0], *_EV_COLS)
                counts[table] += _copy(
                    conn,
                    table,
                    cols,
                    (
                        (*to_values(r), tenant_id, borrower_id, orders_pull,
                         f"invoice={r.source_ref}")
                        for r in rows2
                    ),
                )  # fmt: skip

    result.rows_by_table = dict(counts)
    return result
