"""A Strands model provider over our own `ModelClient`.

Strands drives the agent loop; every model call still goes through `agent/llm.py` (the only
vendor-aware module), wrapped in `LoggedClient`, so each step leaves a `model_call` row with its
cost, prompt caching stays as configured there, and `ScriptedClient` drives Strands in tests.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from tangible.agent.llm import ModelClient, ModelResponse

T = TypeVar("T", bound=BaseModel)


def _result_text(content: list[dict[str, Any]]) -> str:
    parts = []
    for c in content:
        if "text" in c:
            parts.append(c["text"])
        elif "json" in c:
            parts.append(json.dumps(c["json"], ensure_ascii=False))
    return "".join(parts)


def to_neutral(messages: list[Message]) -> list[dict[str, Any]]:
    """Strands (Converse-shaped) messages -> the vendor-neutral shape documented in llm.py."""
    out: list[dict[str, Any]] = []
    for m in messages:
        blocks: list[dict[str, Any]] = []
        for b in m["content"]:
            raw: dict[str, Any] = dict(b)
            if "text" in raw:
                blocks.append({"type": "text", "text": raw["text"]})
            elif "toolUse" in raw:
                tu = raw["toolUse"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tu["toolUseId"],
                        "name": tu["name"],
                        "input": tu["input"],
                    }
                )
            elif "toolResult" in raw:
                tr = raw["toolResult"]
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tr["toolUseId"],
                        "content": _result_text(list(tr["content"])),
                        "is_error": tr.get("status") == "error",
                    }
                )
            # Other block kinds (cache points, reasoning) carry nothing our clients send.
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            # A plain prompt, as the classic loop sends it (same request on the wire).
            out.append({"role": m["role"], "content": blocks[0]["text"]})
        else:
            out.append({"role": m["role"], "content": blocks})
    return out


def to_tool_defs(
    specs: list[ToolSpec] | None, originals: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]] | None:
    """Strands normalises tool schemas (it adds a "Property x" description to every field);
    send our own definitions instead when we have them, so the model sees what classic sends."""
    if not specs:
        return None
    if originals:
        return [originals.get(s["name"]) or to_tool_defs([s])[0] for s in specs]  # type: ignore[index]
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "input_schema": s["inputSchema"]["json"],
        }
        for s in specs
    ]


def to_events(resp: ModelResponse) -> list[StreamEvent]:
    """One complete response replayed as the stream events Strands expects."""
    events: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    if resp.text:
        events += [
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": resp.text}}},
            {"contentBlockStop": {}},
        ]
    for c in resp.tool_calls:
        events += [
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": c["id"], "name": c["name"]}}
                }
            },
            {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(c["input"])}}}},
            {"contentBlockStop": {}},
        ]
    # Same rule as the classic loop: a turn with no tool calls ends the run.
    events.append({"messageStop": {"stopReason": "tool_use" if resp.tool_calls else "end_turn"}})
    total_in = resp.input_tokens + resp.cache_read_tokens + resp.cache_write_tokens
    events.append(
        {
            "metadata": {
                "usage": {
                    "inputTokens": resp.input_tokens,
                    "outputTokens": resp.output_tokens,
                    "totalTokens": total_in + resp.output_tokens,
                    "cacheReadInputTokens": resp.cache_read_tokens,
                    "cacheWriteInputTokens": resp.cache_write_tokens,
                },
                "metrics": {"latencyMs": 0},
            }
        }
    )
    return events  # type: ignore[return-value]


class ModelClientModel(Model):
    """Strands `Model` backed by a `ModelClient` (normally a `LoggedClient`)."""

    def __init__(self, client: ModelClient, tools: list[dict[str, Any]] | None = None) -> None:
        self.client = client
        self.tools = {t["name"]: t for t in tools or []}
        self.model_id = ""  # last model id the client reported
        self.config: dict[str, Any] = {}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        resp = await asyncio.to_thread(
            self.client.complete,
            system=system_prompt or "",
            messages=to_neutral(messages),
            tools=to_tool_defs(tool_specs, self.tools),
        )
        self.model_id = resp.model_id or self.model_id
        for event in to_events(resp):
            yield event

    def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        raise NotImplementedError("Tangible agents do not use structured output")
