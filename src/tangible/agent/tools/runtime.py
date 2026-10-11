"""Tool runtime: registry, argument validation, logging. Every call leaves one tool_call row.

The agent reaches data only through `call_tool`. Errors go back to the model as a message only.
"""

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tangible.agent.llm import ModelClient
from tangible.agent.snapshot import SnapshotInfo, build_snapshot
from tangible.db.cases import Case
from tangible.db.repo import load_ledger
from tangible.db.session import Conn
from tangible.evidence.store import EvidenceStore
from tangible.ledger.types import Ledger
from tangible.policy import Policy

Verifier = Callable[[str, list[object]], bool]


class ToolError(Exception):
    """A tool failure whose message is safe to show the model."""


@dataclass
class ToolContext:
    conn: Conn
    case: Case
    ledger: Ledger
    known: Ledger
    snapshot: SnapshotInfo
    metric_set_id: str
    check_set_id: str
    verifier: Verifier | None = None
    # Wave 3: read_document runs a separate, tool-less model call over files in the store;
    # web_lookup goes through an allow-listed provider. None = tool unavailable.
    document_client: ModelClient | None = None
    evidence_store: EvidenceStore | None = None
    web_provider: Any = None
    # Intangibles v1: place_search / web_search / rdap_lookup go through a SignalProvider
    # (tangible.web.signals; fixtures only for now). None = those tools report "not available".
    signal_provider: Any = None


ToolFn = Callable[[ToolContext, dict[str, Any]], Any]
SideEffect = Callable[[ToolContext, str, Any], None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: int
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    side_effect: SideEffect | None = None
    # Narrows input_schema for one case (e.g. an enum of the keys in the pinned sets).
    schema_for: Callable[[ToolContext], dict[str, Any]] | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    result: Any = None
    error: str | None = None


TOOLS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in TOOLS:
        raise ValueError(f"tool {spec.name} already registered")
    TOOLS[spec.name] = spec
    return spec


def _schema(spec: ToolSpec, ctx: ToolContext | None) -> dict[str, Any]:
    if ctx is not None and spec.schema_for is not None:
        return spec.schema_for(ctx)
    return spec.input_schema


def tool_definitions(ctx: ToolContext | None = None) -> list[dict[str, Any]]:
    """With `ctx`, schemas are narrowed to that case (the same schemas call_tool enforces)."""
    return [
        {"name": t.name, "description": t.description, "input_schema": _schema(t, ctx)}
        for t in TOOLS.values()
    ]


def open_context(
    conn: Conn,
    case: Case,
    policy: Policy,
    workdir: Path,
    verifier: Verifier | None = None,
    *,
    document_client: ModelClient | None = None,
    evidence_store: EvidenceStore | None = None,
    web_provider: Any = None,
    signal_provider: Any = None,
) -> ToolContext:
    """Load the borrower's ledger, freeze the as-of view into a snapshot under `workdir`."""
    if policy.parsed is None:
        raise ValueError("policy has no parsed spec")
    ledger = load_ledger(conn, case.borrower_id)
    known = ledger.known(case.as_of)
    snapshot = build_snapshot(known, workdir / f"snapshot-{case.id}.sqlite", case.as_of)
    return ToolContext(
        conn, case, ledger, known, snapshot,
        policy.parsed.metric_set, policy.parsed.check_set, verifier,
        document_client, evidence_store, web_provider, signal_provider,
    )  # fmt: skip


# ---------------------------------------------------------------- validation

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate(value: Any, schema: dict[str, Any], path: str = "args") -> str | None:
    """Small JSON-schema subset: type, enum, required, properties, additionalProperties false,
    items, minItems, maxItems, minLength, maxLength, pattern. Returns an error or None."""
    t = schema.get("type")
    if t is not None:
        ok = isinstance(value, _TYPES[t])
        if isinstance(value, bool) and t in ("integer", "number"):
            ok = False
        if not ok:
            return f"{path}: expected {t}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: must be one of {schema['enum']}"
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{path}: too short"
        if len(value) > schema.get("maxLength", len(value)):
            return f"{path}: too long"
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            return f"{path}: does not match {schema['pattern']}"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{path}: needs at least {schema['minItems']} items"
        if len(value) > schema.get("maxItems", len(value)):
            return f"{path}: at most {schema['maxItems']} items"
        if "items" in schema:
            for i, v in enumerate(value):
                if (e := validate(v, schema["items"], f"{path}[{i}]")) is not None:
                    return e
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", ()):
            if k not in value:
                return f"{path}: missing required '{k}'"
        for k, v in value.items():
            if k in props:
                if (e := validate(v, props[k], f"{path}.{k}")) is not None:
                    return e
            elif schema.get("additionalProperties") is False:
                return f"{path}: unexpected property '{k}'"
    return None


# ---------------------------------------------------------------- normalisation


def normalise(value: Any) -> Any:
    """Make a tool result JSON-safe: Decimal -> str, dates -> ISO, tuples -> lists."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalise(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {k: normalise(getattr(value, k)) for k in value.__dataclass_fields__}
    raise TypeError(f"result not JSON-serialisable: {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- call


def _log(
    ctx: ToolContext,
    call_id: str,
    tool: str,
    version: int,
    args: Any,
    result: Any,
    error: str | None,
    started: datetime,
    duration_ms: int,
) -> None:
    try:
        args_json = canonical(normalise(args))
    except TypeError:
        args_json = canonical({"_unserialisable": repr(args)[:2000]})
    if args_json == "null" or not isinstance(args, dict):
        args_json = canonical({"_raw": args_json})
    result_json = canonical(result) if error is None else None
    sha = hashlib.sha256(result_json.encode()).hexdigest() if result_json is not None else None
    ctx.conn.execute(
        "INSERT INTO tool_call (id, tenant_id, case_id, tool, tool_version, args, result, "
        "result_sha256, error, started_at, duration_ms) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)",
        (
            call_id, ctx.case.tenant_id, ctx.case.id, tool, version, args_json, result_json,
            sha, error, started, duration_ms,
        ),
    )  # fmt: skip


def call_tool(ctx: ToolContext, name: str, args: Any) -> ToolResult:
    """Validate, run and log one tool call. Never raises for tool failures."""
    call_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    spec = TOOLS.get(name)
    result: Any = None
    error: str | None = None
    if spec is None:
        error = f"unknown tool '{name}'"
    elif (e := validate(args, _schema(spec, ctx))) is not None:
        error = f"invalid arguments: {e}"
    else:
        try:
            with ctx.conn.transaction():  # savepoint: a failed tool leaves the txn usable
                result = normalise(spec.fn(ctx, args))
        except ToolError as e:
            error = str(e) or "tool failed"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _log(ctx, call_id, name, spec.version if spec else 0, args, result, error, started, duration_ms)
    if error is None and spec is not None and spec.side_effect is not None:
        try:
            with ctx.conn.transaction():
                spec.side_effect(ctx, call_id, result)
        except Exception as e:
            # The call row is already written (and append-only); tell the model it did not stick.
            return ToolResult(call_id, None, f"{type(e).__name__}: {e}")
    return ToolResult(call_id, result, error)
