"""record_finding: the agent states a finding and cites the tool calls that support it.

There is deliberately no parameter for a band, limit, or metric value: the agent never sets one.
"""

import uuid
from typing import Any

from tangible.agent.tools.runtime import ToolContext, ToolError, ToolSpec, register
from tangible.agent.verify import verify_numbers
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


def _replaceable(ctx: ToolContext, raw: str) -> str:
    """The canonical id of an earlier finding this one may replace; raises ToolError otherwise.

    Only the agent's own findings of this case, not yet reviewed by an analyst, not already
    replaced. "Earlier" holds by
    construction: the new row does not exist yet, so any finding found here predates it, whether
    from this run or a previous one."""
    try:
        old = str(uuid.UUID(raw))
    except ValueError:
        raise ToolError(f"replaces {raw!r} is not a valid finding id") from None
    row = ctx.conn.execute("SELECT origin, case_id FROM finding WHERE id = %s", (old,)).fetchone()
    if row is None or str(row[1]) != ctx.case.id:
        raise ToolError(f"replaces: {old} is not a finding of this case")
    if row[0] != "model":
        raise ToolError(
            f"replaces: {old} is a rule finding; only findings you recorded can be replaced"
        )
    reviewed = ctx.conn.execute(
        "SELECT action FROM underwriter_action WHERE finding_id = %s ORDER BY at DESC LIMIT 1",
        (old,),
    ).fetchone()
    if reviewed is not None:
        # Once an analyst has acted on a finding it is theirs: replacing it would drop an accepted
        # hold from the policy inputs. Record a new finding instead and let the analyst decide.
        raise ToolError(
            f"replaces: {old} has been reviewed by an analyst ({reviewed[0]}); it cannot be "
            "replaced. Record a new finding and say what it adds"
        )
    newer = ctx.conn.execute("SELECT id FROM finding WHERE replaces_id = %s", (old,)).fetchone()
    if newer is not None:
        raise ToolError(
            f"replaces: {old} has already been replaced by finding {newer[0]}; "
            "replace that one if it needs another correction"
        )
    return old


def record_finding(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ids = [_canon(i) for i in args["tool_call_ids"]]
    _cited(ctx, ids)
    replaces = _replaceable(ctx, args["replaces"]) if "replaces" in args else None
    return {
        "recorded": True,
        # Allocated here so the model can be told the id (and cite it in `replaces` later);
        # the side effect inserts the row under it.
        "finding_id": str(uuid.uuid4()),
        "replaces": replaces,
        "check_key": f"agent:{args['slug']}",
        "check_version": 1,
        "severity": args["severity"],
        "pillar": args["pillar"],
        "direction": args["direction"],
        "summary": args["summary"],
        "tool_call_ids": ids,
        "evidence_refs": list(args.get("evidence_refs", [])),
    }


def verification_feedback(ctx: ToolContext, result: dict[str, Any]) -> str:
    """What the model is told after record_finding. Display only; nothing here is stored.

    Re-runs the (pure) verifier on the same cited results `_insert` used, so the text agrees
    with the stored `verified` flag.
    """
    if ctx.verifier is None:
        return "recorded"
    cited = _cited(ctx, result["tool_call_ids"])
    if ctx.verifier(result["summary"], cited):
        return "recorded; verified"
    unmatched = verify_numbers(result["summary"], cited).unmatched
    if not unmatched:
        return "recorded; UNVERIFIED"
    return (
        "recorded; UNVERIFIED: numbers not found in cited results: "
        + ", ".join(dict.fromkeys(unmatched))
        + ". This finding stays on file marked unverified. If it matters, compute those numbers "
        "with run_analysis or query_ledger and record a corrected finding citing that call, with "
        f"replaces set to {result['finding_id']}."
    )


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
        replaces_id=result["replaces"], finding_id=result["finding_id"],
    )  # fmt: skip


register(
    ToolSpec(
        "record_finding",
        2,
        "Record a finding for the analyst. Cite the tool_call_ids (successful calls in this case) "
        "whose results support it. Every number in the summary must appear in a cited result; "
        "compute derived numbers first with run_analysis or query_ledger and cite that call. "
        "The result says 'recorded; verified' or 'recorded; UNVERIFIED: numbers not found in "
        "cited results: ...'. Record only findings that raise or lower risk or need analyst "
        "action, not 'checked and clean' ones. The result gives the new finding_id. To correct "
        "an earlier finding of yours (for example after UNVERIFIED), record the corrected one "
        "with `replaces` set to that earlier finding_id: the correction takes its place and the "
        "old one is shown as superseded. Never re-record a near-duplicate without `replaces`. "
        "You cannot set bands, limits or numbers; policy decides those.",
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
                "replaces": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["summary", "severity", "pillar", "direction", "tool_call_ids", "slug"],
            "additionalProperties": False,
        },
        record_finding,
        _insert,
    )
)
