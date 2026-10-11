"""The Strands Agents engine for investigations (TANGIBLE_AGENT_ENGINE=strands, the default)."""

from typing import Any

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.tools.executors import SequentialToolExecutor

from tangible.agent.loop import WRAP_UP_MAX_CHARS, RunOutcome, RunSetup
from tangible.agent.strands_engine.guardrails import RunState, TangibleGuardrails, tangible_tools
from tangible.agent.strands_engine.model import ModelClientModel

LEAD = "lead_investigator"


def build_agent(
    state: RunState,
    name: str,
    system: str,
    tool_names: set[str] | None = None,
    max_steps: int | None = None,
) -> Agent:
    """One Strands agent wired to Tangible: our model client, our tools, our guardrails."""
    tools: list[Any] = list(tangible_tools(state, tool_names))
    allowed = {t.tool_name for t in tools}
    return Agent(
        name=name,
        model=ModelClientModel(state.setup.logged, state.setup.tools),
        system_prompt=system,
        tools=tools,
        hooks=[TangibleGuardrails(state, name, allowed, max_steps)],
        # Tools share one DB connection and savepoints: never run them concurrently.
        tool_executor=SequentialToolExecutor(),
        # The full history is the audit trail of what the model saw: never trim or summarise it.
        conversation_manager=NullConversationManager(),
        context_manager=False,
        retry_strategy=None,
        callback_handler=None,
    )


def run_strands(setup: RunSetup) -> RunOutcome:
    state = RunState(setup)
    setup.logged.lock = state.lock
    lead = build_agent(state, LEAD, setup.system)
    result = lead(setup.first_message)
    model = lead.model
    model_id = model.model_id if isinstance(model, ModelClientModel) else ""
    wrap_up = ""
    if state.stop_reason is None:
        wrap_up = "".join(c.get("text", "") for c in result.message["content"]).strip()
    return RunOutcome(
        state.stop_reason or "end_turn",
        state.steps,
        model_id,
        wrap_up[:WRAP_UP_MAX_CHARS],
        {"agents": dict(state.agent_steps)},
    )
