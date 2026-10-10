"""query_ledger: read-only SQL over the frozen snapshot."""

import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tangible.agent.tools.runtime import ToolContext, ToolError, ToolSpec, register

ROW_CAP = 1000
TIME_LIMIT_S = 5.0
# Recursive CTEs are allowed; the progress handler bounds them.
_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def open_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.set_authorizer(_authorizer)
    return con


def query_ledger(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + TIME_LIMIT_S
    con = open_readonly(ctx.snapshot.path)
    try:
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = con.execute(args["sql"])
            columns = [d[0] for d in cur.description or ()]
            rows = cur.fetchmany(ROW_CAP + 1)
        except sqlite3.Error as e:
            if "interrupted" in str(e):
                raise ToolError(f"query aborted: exceeded {TIME_LIMIT_S:g}s") from e
            raise ToolError(f"sql error: {e}") from e
    finally:
        con.close()
    truncated = len(rows) > ROW_CAP
    out = [[v.hex() if isinstance(v, bytes) else v for v in r] for r in rows[:ROW_CAP]]
    return {"columns": columns, "rows": out, "truncated": truncated}


register(
    ToolSpec(
        "query_ledger",
        1,
        "Run one read-only SQLite SELECT over the frozen ledger snapshot (tables: orders, "
        "refunds, payouts, bank_txns, invoices, inventory_snapshots, account_balances; "
        "_meta describes columns). Decimals are TEXT; use the *_minor/_micro integer columns "
        "for exact sums. Max 1000 rows, 5 s.",
        {
            "type": "object",
            "properties": {"sql": {"type": "string", "minLength": 1, "maxLength": 20000}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        query_ledger,
    )
)
