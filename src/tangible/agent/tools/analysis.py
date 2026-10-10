"""run_analysis: agent-written Python in the sandbox, over the snapshot. Writes nothing."""

from typing import Any

from tangible.agent import sandbox
from tangible.agent.tools.runtime import ToolContext, ToolSpec, register


def run_analysis(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    res = sandbox.run_python(args["code"], ctx.snapshot.path)
    return {
        "result": res.result,
        "stdout": res.stdout,
        "error": res.error,
        "duration_ms": res.duration_ms,
    }


register(
    ToolSpec(
        "run_analysis",
        1,
        "Run Python in a no-network sandbox with read-only access to the ledger snapshot "
        "(path in SNAPSHOT_DB). Assign a JSON-serialisable value to RESULT.",
        {
            "type": "object",
            "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 50000}},
            "required": ["code"],
            "additionalProperties": False,
        },
        run_analysis,
    )
)
