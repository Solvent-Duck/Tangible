"""compute_metric and run_check: the versioned library, resolved through the pinned sets."""

from typing import Any

from tangible.agent.tools.runtime import ToolContext, ToolError, ToolSpec, register
from tangible.checks.registry import REGISTRY as CHECKS
from tangible.checks.registry import run_check as _run_check
from tangible.checks.sets import check_set
from tangible.metrics.registry import REGISTRY as METRICS
from tangible.metrics.registry import compute
from tangible.metrics.sets import metric_set


def _about(fn: Any) -> str:
    """First paragraph of the function's docstring, on one line ('' if none)."""
    doc = (fn.__doc__ or "").strip()
    return " ".join(doc.split("\n\n", 1)[0].split())


def library_catalog(metric_set_id: str, check_set_id: str) -> dict[str, Any]:
    """The metric and check keys a case can use, from its pinned sets (for the model's prompts)."""

    def entry(key: str, version: int, fn: Any) -> dict[str, Any]:
        about = _about(fn)
        return {"key": key, "version": version, **({"about": about} if about else {})}

    return {
        "metric_set": metric_set_id,
        "metrics": [entry(k, v, METRICS[(k, v)].fn) for k, v in metric_set(metric_set_id)],
        "check_set": check_set_id,
        "checks": [entry(k, v, CHECKS[(k, v)].fn) for k, v in check_set(check_set_id)],
    }


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


def _key_enum(keys: list[str]) -> dict[str, Any]:
    return {**_KEY, "properties": {"key": {"type": "string", "enum": keys}}}


register(
    ToolSpec(
        "compute_metric",
        1,
        "Compute one released metric (pinned version) for this case as of its date.",
        _KEY,
        compute_metric,
        schema_for=lambda ctx: _key_enum([k for k, _ in metric_set(ctx.metric_set_id)]),
    )
)
register(
    ToolSpec(
        "run_check",
        1,
        "Run one released integrity check for this case as of its date; returns its findings.",
        _KEY,
        run_check,
        schema_for=lambda ctx: _key_enum([k for k, _ in check_set(ctx.check_set_id)]),
    )
)
