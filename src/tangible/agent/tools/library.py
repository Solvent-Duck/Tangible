"""compute_metric and run_check: the versioned library, resolved through the pinned sets."""

from typing import Any

from tangible.agent.tools.runtime import ToolContext, ToolError, ToolSpec, register
from tangible.checks.registry import run_check as _run_check
from tangible.checks.sets import check_set
from tangible.metrics.registry import compute
from tangible.metrics.sets import metric_set


def compute_metric(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args["key"]
    version = dict(metric_set(ctx.metric_set_id)).get(key)
    if version is None:
        raise ToolError(f"unknown metric '{key}' in set {ctx.metric_set_id}")
    value = compute(key, version, ctx.ledger, ctx.case.as_of)
    return {"key": key, "version": version, "as_of": ctx.case.as_of, "value": value}


def run_check(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args["key"]
    version = dict(check_set(ctx.check_set_id)).get(key)
    if version is None:
        raise ToolError(f"unknown check '{key}' in set {ctx.check_set_id}")
    drafts = _run_check(key, version, ctx.ledger, ctx.case.as_of)
    return {
        "key": key,
        "version": version,
        "as_of": ctx.case.as_of,
        "findings": [
            {
                "check_key": d.check_key,
                "check_version": d.check_version,
                "severity": d.severity,
                "pillar": d.pillar,
                "direction": d.direction,
                "summary": d.summary,
                "evidence_refs": list(d.evidence_refs),
                "facts": dict(d.facts),
            }
            for d in drafts
        ],
    }


_KEY = {
    "type": "object",
    "properties": {"key": {"type": "string", "minLength": 1, "maxLength": 100}},
    "required": ["key"],
    "additionalProperties": False,
}

register(
    ToolSpec(
        "compute_metric",
        1,
        "Compute one released metric (pinned version) for this case as of its date.",
        _KEY,
        compute_metric,
    )
)
register(
    ToolSpec(
        "run_check",
        1,
        "Run one released integrity check for this case as of its date; returns its findings.",
        _KEY,
        run_check,
    )
)
