"""Presentation helpers for the trace page: shape JSON into tables and short labels.

Pure functions, no I/O. Every returned string is plain text; the Jinja templates escape it.
Tool args and results can hold borrower or web text, so nothing here builds HTML.
"""

import json
from typing import Any

MAX_ROWS = 50
MAX_CELL = 2000
# Args holding multi-line source text: shown in their own <pre>, not in a table cell.
_CODE_ARGS = ("sql", "code")

Scalar = str | int | float | bool | None


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, str | int | float | bool)


def _text(v: Scalar) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + f"... ({len(s) - MAX_CELL} more characters)"


def _cell(v: Scalar) -> dict[str, Any]:
    text = _text(v)
    return {"text": text, "block": isinstance(v, str) and "\n" in v}


def _pretty(v: Any) -> str:
    return json.dumps(v, indent=2, sort_keys=True, default=str)


def _table(columns: list[str], rows: list[list[Scalar]], extra: int = 0) -> dict[str, Any]:
    shown = rows[:MAX_ROWS]
    return {
        "kind": "table",
        "columns": columns,
        "rows": [[_cell(c) for c in r] for r in shown],
        "more": max(len(rows) - len(shown), 0) + extra,
    }


def _flat_row_dicts(v: Any) -> bool:
    return (
        isinstance(v, list)
        and bool(v)
        and all(isinstance(r, dict) and all(_is_scalar(x) for x in r.values()) for r in v)
    )


def _columns_rows(v: dict[str, Any]) -> bool:
    """query_ledger's result shape: {"columns": [...], "rows": [[...], ...], ...}."""
    cols, rows = v.get("columns"), v.get("rows")
    return (
        isinstance(cols, list)
        and all(isinstance(c, str) for c in cols)
        and isinstance(rows, list)
        and all(isinstance(r, list) and all(_is_scalar(x) for x in r) for r in rows)
    )


def view(v: Any) -> dict[str, Any]:
    """Shape a JSON value for display.

    kinds: "empty", "scalar" {cell}, "kv" {rows: [(key, cell)], sections: [(name, text)]},
    "table" {columns, rows, more}, "tables" {head: kv-or-None, table}, "pre" {text}.
    """
    if v is None or v == {} or v == []:
        return {"kind": "empty", "text": "none" if v is None else ("{}" if v == {} else "[]")}
    if _is_scalar(v):
        return {"kind": "scalar", "cell": _cell(v)}
    if isinstance(v, dict) and _columns_rows(v):
        rest = {k: x for k, x in v.items() if k not in ("columns", "rows")}
        table = _table(v["columns"], v["rows"])
        if rest and all(_is_scalar(x) for x in rest.values()):
            return {"kind": "tables", "head": view(rest), "table": table}
        if rest:
            return {"kind": "pre", "text": _pretty(v)}
        return table
    if isinstance(v, dict) and all(_is_scalar(x) for x in v.values()):
        return {"kind": "kv", "rows": [(str(k), _cell(x)) for k, x in v.items()]}
    if _flat_row_dicts(v):
        cols: list[str] = list(dict.fromkeys(str(k) for r in v for k in r))
        return _table(cols, [[r.get(c) for c in cols] for r in v])
    return {"kind": "pre", "text": _pretty(v)}


def args_view(args: Any) -> dict[str, Any]:
    """Tool arguments: `sql` and `code` strings pulled out into code blocks, the rest a table."""
    blocks: list[tuple[str, str]] = []
    rest = args
    if isinstance(args, dict):
        blocks = [(k, args[k]) for k in _CODE_ARGS if isinstance(args.get(k), str)]
        rest = {k: x for k, x in args.items() if k not in {b[0] for b in blocks}}
    return {"blocks": blocks, "rest": view(rest) if rest or not blocks else None}


def short_ref(ref: str) -> str:
    """`<uuid>#row=1234` -> `pull 1a2b3c4d · row 1234`. The full ref belongs in a title attr."""
    pull, sep, locator = str(ref).partition("#")
    if not sep or not pull or not locator:
        return str(ref)
    kind, eq, arg = locator.partition("=")
    where = f"{kind} {arg}" if eq else locator
    return f"pull {pull[:8]} · {where}"
