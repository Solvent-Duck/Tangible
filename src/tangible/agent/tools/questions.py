"""draft_borrower_question: stores a draft. Nothing is sent; an analyst approves separately."""

from typing import Any

from tangible.agent.tools.runtime import ToolContext, ToolSpec, register


def draft_question(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"drafted": True, "body": args["body"]}


def _insert(ctx: ToolContext, call_id: str, result: Any) -> None:
    ctx.conn.execute(
        "INSERT INTO borrower_question (tenant_id, case_id, body, drafted_by_tool_call) "
        "VALUES (%s, %s, %s, %s)",
        (ctx.case.tenant_id, ctx.case.id, result["body"], call_id),
    )


register(
    ToolSpec(
        "draft_borrower_question",
        1,
        "Draft a question for the borrower. It is only a draft; an analyst must approve it "
        "before anything is sent.",
        {
            "type": "object",
            "properties": {"body": {"type": "string", "minLength": 1, "maxLength": 4000}},
            "required": ["body"],
            "additionalProperties": False,
        },
        draft_question,
        _insert,
    )
)
