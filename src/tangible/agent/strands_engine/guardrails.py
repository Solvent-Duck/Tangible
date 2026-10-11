"""Tangible's invariants as Strands hooks: the agent investigates, tools compute, policy decides.

`TangibleGuardrails` is attached to every Strands agent in a run. It enforces what the model
must never decide for itself:

- budgets: the case's step and cost budgets are shared by every agent in the run and checked
  before each model call (`BeforeModelCallEvent.cancel` ends the turn cleanly);
- scope: an agent can only run the tools it was given (`BeforeToolCallEvent.cancel_tool`);
- untrusted data: every tool result, including Strands' own error results, reaches the model
  only as a quoted `<tool_output untrusted="true">` block (`AfterToolCallEvent.result`);
- wrap-up: when the budget is nearly spent or tools keep failing, a trusted notice asks the
  model to stop (`AfterToolsEvent`), exactly as the classic loop does.

Tool execution itself is `call_tool` (validated, savepointed, one `tool_call` row per call).
"""

import json
import threading
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    AfterToolsEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolUse

from tangible.agent.loop import RunSetup, tool_payload, wrap_tool_output, wrap_up_notice
from tangible.agent.progress import FEED
from tangible.agent.strands_engine.model import _result_text
from tangible.agent.tools import call_tool
from tangible.agent.tools.runtime import TOOLS

STOP_TEXT = "Investigation stopped by the loop: {why}."


@dataclass
class RunState:
    """Shared by every agent in one investigation run."""

    setup: RunSetup
    # One DB connection serves the whole run; parallel agents take turns on it.
    lock: threading.RLock = field(default_factory=threading.RLock)
    steps: int = 0
    agent_steps: Counter[str] = field(default_factory=Counter)
    stop_reason: str | None = None  # step_budget | cost_budget once a budget is spent
    call_ids: dict[tuple[int, str], str] = field(
        default_factory=dict
    )  # (agent, toolUseId) -> call_id
    events: list[dict[str, Any]] = field(default_factory=list)  # display-only progress trail
    tool_calls: Counter[str] = field(default_factory=Counter)  # agent -> tool calls run

    def note(self, agent: str, kind: str, text: str) -> None:
        with self.lock:
            self.events.append({"agent": agent, "kind": kind, "text": text})
        FEED.add(self.setup.case.id, agent, kind, text)


def tangible_tools(state: RunState, names: set[str] | None = None) -> list[PythonAgentTool]:
    """The case's Tangible tools as Strands tools (case-narrowed schemas), all via call_tool."""
    return [
        _tool(state, d["name"], d["description"], d["input_schema"])
        for d in state.setup.tools
        if names is None or d["name"] in names
    ]


def _tool(state: RunState, name: str, description: str, schema: dict[str, Any]) -> PythonAgentTool:
    ctx = state.setup.ctx

    def fn(tool_use: ToolUse, **inv: Any) -> ToolResult:
        with state.lock:
            result = call_tool(ctx, name, tool_use["input"])
            payload, is_error = tool_payload(ctx, name, result)
            state.call_ids[(id(inv.get("agent")), tool_use["toolUseId"])] = result.call_id
            state.tool_calls[getattr(inv.get("agent"), "name", "agent")] += 1
        if name == "record_finding" and not is_error:
            agent = getattr(inv.get("agent"), "name", "agent")
            state.note(agent, "finding", json.loads(payload).get("status", "recorded"))
        return {
            "toolUseId": tool_use["toolUseId"],
            "status": "error" if is_error else "success",
            "content": [{"text": payload}],
        }

    fn.__name__ = name
    # A copy: Strands normalises specs in place, and `schema` is the one call_tool validates with.
    spec = {"name": name, "description": description, "inputSchema": {"json": deepcopy(schema)}}
    return PythonAgentTool(name, spec, fn)  # type: ignore[arg-type]


class TangibleGuardrails(HookProvider):
    def __init__(
        self, state: RunState, agent_name: str, allowed: set[str], max_steps: int | None = None
    ) -> None:
        self.state = state
        self.agent_name = agent_name
        self.allowed = allowed
        self.max_steps = max_steps  # this agent's own cap inside the shared budget
        self.stalled = 0  # consecutive tool batches that returned only errors
        self.notified: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        s, case = self.state, self.state.setup.case
        with s.lock:
            if s.stop_reason is None and s.steps >= case.budget_steps:
                s.stop_reason = "step_budget"
            if s.stop_reason is None and s.setup.logged.cost >= case.budget_usd:
                s.stop_reason = "cost_budget"
            if s.stop_reason is not None:
                why = s.stop_reason.replace("_", " ") + " spent"
                event.cancel = STOP_TEXT.format(why=why)
                s.note(self.agent_name, "stop", why)
                return
            if self.max_steps is not None and s.agent_steps[self.agent_name] >= self.max_steps:
                event.cancel = STOP_TEXT.format(why=f"{self.agent_name} used its steps")
                return
            s.steps += 1
            s.agent_steps[self.agent_name] += 1

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name in self.allowed:
            return
        if name in TOOLS:  # a real tool outside this agent's scope
            event.cancel_tool = f"tool '{name}' is not available to {self.agent_name}"
            self.state.note(self.agent_name, "blocked", f"blocked {name}")
        else:  # an invented tool: call_tool rejects it and leaves the audit row, as classic does
            event.selected_tool = _tool(self.state, name, "", {})

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tu, result = event.tool_use, event.result
        with self.state.lock:
            call_id = self.state.call_ids.pop((id(event.agent), tu["toolUseId"]), tu["toolUseId"])
        payload = _result_text(list(result["content"]))  # type: ignore[arg-type]
        event.result = {
            **result,
            "content": [{"text": wrap_tool_output(call_id, tu["name"], payload)}],
        }
        failed = result["status"] == "error"
        self.state.note(self.agent_name, "error" if failed else "tool", tu["name"])

    def after_tools(self, event: AfterToolsEvent) -> None:
        results = [c["toolResult"] for c in event.message["content"] if "toolResult" in c]
        self.stalled = self.stalled + 1 if all(r["status"] == "error" for r in results) else 0
        with self.state.lock:
            steps = self.state.steps
        text = wrap_up_notice(self.state.setup.case, steps, self.stalled, self.notified)
        if text is not None:
            # The message is appended to the agent's history after this hook returns.
            event.message["content"].append({"text": text})
