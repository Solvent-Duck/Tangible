"""Case files: one investigation of a borrower as of a date. Use a `tenant_session` connection."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tangible.db.session import Conn


@dataclass(frozen=True)
class Case:
    id: str
    tenant_id: str
    borrower_id: str
    as_of: datetime
    loan_amount: Decimal | None
    budget_steps: int
    budget_usd: Decimal
    assigned_analyst: str | None
    status: str


_COLS = (
    "id, tenant_id, borrower_id, as_of, loan_amount, budget_steps, budget_usd, "
    "assigned_analyst, status"
)


def create_case(
    conn: Conn,
    tenant_id: str,
    borrower_id: str,
    as_of: datetime,
    *,
    loan_amount: Decimal | None = None,
    budget_steps: int = 40,
    budget_usd: Decimal = Decimal("5.00"),
    assigned_analyst: str | None = None,
) -> Case:
    row = conn.execute(
        f"INSERT INTO case_file (tenant_id, borrower_id, as_of, loan_amount, budget_steps, "
        f"budget_usd, assigned_analyst) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (tenant_id, borrower_id, as_of, loan_amount, budget_steps, budget_usd, assigned_analyst),
    ).fetchone()
    assert row
    return _case(row)


def get_case(conn: Conn, case_id: str) -> Case:
    row = conn.execute(f"SELECT {_COLS} FROM case_file WHERE id = %s", (case_id,)).fetchone()
    if row is None:
        raise KeyError(f"case {case_id} not found")
    return _case(row)


def set_case_status(conn: Conn, case_id: str, status: str) -> None:
    conn.execute("UPDATE case_file SET status = %s WHERE id = %s", (status, case_id))


def _case(r: tuple[Any, ...]) -> Case:
    return Case(str(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5], r[6], r[7], r[8])
