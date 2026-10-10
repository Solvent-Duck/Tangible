"""Reads and small writes against the tenant-scoped schema. Use a `tenant_session` connection."""

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Literal, cast

from tangible.db.session import Conn
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import (
    AccountBalance,
    BankTxn,
    Connection,
    InventorySnapshot,
    Invoice,
    Ledger,
    Order,
    Payout,
    Refund,
    Statement,
    StatementLine,
)


def create_borrower(conn: Conn, tenant_id: str, name: str) -> str:
    row = conn.execute(
        "INSERT INTO borrower (tenant_id, name) VALUES (%s, %s) RETURNING id", (tenant_id, name)
    ).fetchone()
    assert row
    return str(row[0])


def _ev(r: tuple[Any, ...]) -> EvidenceRef:
    return EvidenceRef(str(r[-2]), r[-1])


def _fetch[T](
    conn: Conn, table: str, cols: str, borrower_id: str, build: Callable[[tuple[Any, ...]], T]
) -> tuple[T, ...]:
    rows = conn.execute(
        f"SELECT {cols}, evidence_pull_id, evidence_locator FROM {table} "
        "WHERE borrower_id = %s ORDER BY observed_at, source_ref, id",
        (borrower_id,),
    ).fetchall()
    return tuple(build(r) for r in rows)


def load_ledger(conn: Conn, borrower_id: str) -> Ledger:
    """Every row of every revision; time filtering is `Ledger.known`'s job."""
    b = str(borrower_id)
    orders = _fetch(
        conn, "orders",
        "source_ref, gross, discounts, occurred_at, observed_at, customer_ref, channel, currency",
        b, lambda r: Order(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7].strip(), _ev(r)),
    )  # fmt: skip
    refunds = _fetch(
        conn, "refund", "source_ref, order_ref, amount, occurred_at, observed_at", b,
        lambda r: Refund(r[0], r[1], r[2], r[3], r[4], _ev(r)),
    )  # fmt: skip
    payouts = _fetch(
        conn, "payout", "source_ref, processor, amount, fees, expected_on, settled_on, observed_at",
        b, lambda r: Payout(r[0], r[1], r[2], r[3], r[4], r[5], r[6], _ev(r)),
    )  # fmt: skip
    bank = _fetch(
        conn, "bank_txn",
        "source_ref, account_ref, amount, direction, counterparty, description, posted_on, "
        "observed_at",
        b, lambda r: BankTxn(r[0], r[1], r[2], cast(Literal["in", "out"], r[3]), r[4], r[5], r[6],
                             r[7], _ev(r)),
    )  # fmt: skip
    invoices = _fetch(
        conn, "invoice",
        "source_ref, kind, counterparty, amount, issued_on, due_on, paid_on, observed_at", b,
        lambda r: Invoice(r[0], cast(Literal["payable", "receivable"], r[1]), r[2], r[3], r[4],
                          r[5], r[6], r[7], _ev(r)),
    )  # fmt: skip
    stock = _fetch(
        conn, "inventory_snapshot",
        "source_ref, sku, units_on_hand, unit_cost, as_of, observed_at", b,
        lambda r: InventorySnapshot(r[0], r[1], r[2], r[3], r[4], r[5], _ev(r)),
    )  # fmt: skip
    balances = _fetch(
        conn, "account_balance", "source_ref, account_ref, balance, as_of, observed_at", b,
        lambda r: AccountBalance(r[0], r[1], r[2], r[3], r[4], _ev(r)),
    )  # fmt: skip
    conns = tuple(
        Connection(str(r[0]), r[1], r[2], r[3], r[4], r[2])
        for r in conn.execute(
            "SELECT id, source, granted_at, revoked_at, last_success_at FROM connection "
            "WHERE borrower_id = %s ORDER BY granted_at, id",
            (b,),
        ).fetchall()
    )
    statements = _load_statements(conn, b)
    return Ledger(
        orders=orders, refunds=refunds, payouts=payouts, bank_txns=bank, invoices=invoices,
        inventory_snapshots=stock, account_balances=balances, connections=conns,
        applications=(), statements=statements,
    )  # fmt: skip


def _load_statements(conn: Conn, borrower_id: str) -> tuple[Statement, ...]:
    rows = conn.execute(
        "SELECT source_ref, account_ref, period_start, period_end, opening_balance, "
        "closing_balance, lines, file_traits, observed_at, evidence_pull_id FROM document_extract "
        "WHERE borrower_id = %s ORDER BY observed_at, source_ref, id",
        (borrower_id,),
    ).fetchall()
    out = []
    for r in rows:
        lines = tuple(
            StatementLine(
                date.fromisoformat(x["posted_on"]),
                Decimal(x["amount"]),
                cast(Literal["in", "out"], x["direction"]),
                x["description"],
            )
            for x in r[6]
        )
        traits = tuple(sorted((str(k), str(v)) for k, v in r[7].items()))
        out.append(
            Statement(
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                lines,
                traits,
                r[8],
                EvidenceRef(str(r[9]), "statement"),
            )  # fmt: skip
        )
    return tuple(out)


def insert_statement(
    conn: Conn,
    tenant_id: str,
    borrower_id: str,
    st: Statement,
    *,
    extracted_by_model_call: str | None = None,
) -> str:
    """Append one document_extract row (a re-extraction is a new row, same source_ref)."""
    if st.evidence is None:
        raise ValueError("statement has no evidence pull")
    lines = [
        {
            "posted_on": ln.posted_on.isoformat(),
            "amount": str(ln.amount),
            "direction": ln.direction,
            "description": ln.description,
        }
        for ln in st.lines
    ]
    row = conn.execute(
        "INSERT INTO document_extract (tenant_id, borrower_id, source_ref, evidence_pull_id, "
        "account_ref, period_start, period_end, opening_balance, closing_balance, lines, "
        "file_traits, extracted_by_model_call, observed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
        (
            tenant_id, borrower_id, st.source_ref, st.evidence.pull_id, st.account_ref,
            st.period_start, st.period_end, st.opening_balance, st.closing_balance,
            json.dumps(lines), json.dumps(dict(st.file_traits)), extracted_by_model_call,
            st.observed_at,
        ),
    ).fetchone()  # fmt: skip
    assert row
    return str(row[0])
