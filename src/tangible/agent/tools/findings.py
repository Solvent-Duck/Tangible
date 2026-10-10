"""record_finding: the agent states a finding and cites the tool calls that support it.

There is deliberately no parameter for a band, limit, or metric value: the agent never sets one.
"""

import uuid
from typing import Any

from tangible.agent.tools.runtime import ToolContext, ToolError, ToolSpec, register
from tangible.checks.registry import FindingDraft
from tangible.db.findings import insert_finding

_SLUG = r"[a-z0-9][a-z0-9_-]{0,63}"


def _canon(i: str) -> str:
    try:
        return str(uuid.UUID(i))
    except ValueError:
        raise ToolError(f"tool_call_id {i!r} is not a valid id") from None


def _cited(ctx: ToolContext, ids: list[str]) -> list[Any]:
    """Results of the cited calls; raises ToolError unless each is a clean call of this case."""
    rows = ctx.conn.execute(
        "SELECT id, result, error FROM tool_call WHERE case_id = %s AND id = ANY(%s::uuid[])",
        (ctx.case.id, ids),
    ).fetchall()
    by_id = {str(r[0]): r for r in rows}
    for i in ids:
        r = by_id.get(i)
        if r is None:
            raise ToolError(f"tool_call_id {i} does not exist for this case")
        if r[2] is not None:
            raise ToolError(f"tool_call_id {i} ended in an error and cannot support a finding")
        if isinstance(r[1], dict) and r[1].get("error"):
            raise ToolError(f"tool_call_id {i} reported an error and cannot support a finding")
    return [by_id[i][1] for i in ids]


def record_finding(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ids = [_canon(i) for i in args["tool_call_ids"]]
    _cited(ctx, ids)
    return {
        "recorded": True,
        "check_key": f"agent:{args['slug']}",
        "check_version": 1,
        "severity": args["severity"],
        "pillar": args["pillar"],
        "direction": args["direction"],
        "summary": args["summary"],
        "tool_call_ids": ids,
        "evidence_refs": list(args.get("evidence_refs", [])),
    }


def _insert(ctx: ToolContext, call_id: str, result: Any) -> None:
    ids: list[str] = result["tool_call_ids"]
    verified: bool | None = None
    if ctx.verifier is not None:
        verified = ctx.verifier(result["summary"], _cited(ctx, ids))
    draft = FindingDraft(
        result["check_key"],
        result["check_version"],
        result["severity"],
        result["pillar"],
        result["summary"],
        tuple(result["evidence_refs"]),
        result["direction"],
    )
    insert_finding(
        ctx.conn, ctx.case.tenant_id, ctx.case.borrower_id, draft,
        origin="model", case_id=ctx.case.id, tool_call_ids=ids, verified=verified,
    )  # fmt: skip


register(
    ToolSpec(
        "record_finding",
        1,
        "Record a finding for the analyst. Cite the tool_call_ids (successful calls in this case) "
        "whose results support it. You cannot set bands, limits or numbers; policy decides those.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "severity": {"type": "string", "enum": ["info", "review", "hold"]},
                "pillar": {"type": "string", "enum": ["health", "momentum", "integrity"]},
                "direction": {"type": "string", "enum": ["raises_risk", "lowers_risk"]},
                "tool_call_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
                "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                "slug": {"type": "string", "pattern": _SLUG},
            },
            "required": ["summary", "severity", "pillar", "direction", "tool_call_ids", "slug"],
            "additionalProperties": False,
        },
        record_finding,
        _insert,
    )
)
