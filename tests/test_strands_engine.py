"""The Strands engine: model adapter, guardrail hooks, and parity with the classic loop."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from strands.tools.executors import SequentialToolExecutor

from tangible.agent.llm import LoggedClient, ModelResponse, ScriptedClient
from tangible.agent.loop import RunSetup
from tangible.agent.progress import FEED
from tangible.agent.prompts import load_prompt
from tangible.agent.strands_engine import build_agent
from tangible.agent.strands_engine.guardrails import RunState
from tangible.agent.strands_engine.model import ModelClientModel, to_events, to_neutral
from tangible.agent.tools import open_context, tool_definitions
from tangible.db.findings import load_findings
from tangible.db.session import Conn, tenant_session
from tangible.policy import load_policy
from test_agent_loop import FREE, FnClient, approved_case, call, last_call_id, run, say
from test_assessment import POLICY_PATH, World, datasets, make_world  # noqa: F401
from test_assessment import hidden as hidden_world  # noqa: F401

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# ---------------------------------------------------------------- adapter (no database)


def test_strands_messages_become_neutral_blocks() -> None:
    msgs: Any = [
        {"role": "user", "content": [{"text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "checking"},
                {"toolUse": {"toolUseId": "t1", "name": "run_check", "input": {"key": "k"}}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "t1", "status": "error", "content": [{"text": "x"}]}},
                {"text": "notice"},
                {"cachePoint": {"type": "default"}},
            ],
        },
    ]
    assert to_neutral(msgs) == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "run_check", "input": {"key": "k"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": True},
                {"type": "text", "text": "notice"},
            ],
        },
    ]


def test_response_replays_as_stream_events_with_usage() -> None:
    resp = ModelResponse(
        text="hi",
        tool_calls=({"id": "a", "name": "query_ledger", "input": {"sql": "SELECT 1"}},),
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=100,
        cache_write_tokens=7,
    )
    events: Any = to_events(resp)
    assert events[0] == {"messageStart": {"role": "assistant"}}
    assert {"contentBlockDelta": {"delta": {"text": "hi"}}} in events
    assert {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"sql": "SELECT 1"}'}}}} in events
    assert {"messageStop": {"stopReason": "tool_use"}} in events
    usage = events[-1]["metadata"]["usage"]
    assert usage["cacheReadInputTokens"] == 100 and usage["totalTokens"] == 122
    assert to_events(ModelResponse(text="done"))[-2] == {"messageStop": {"stopReason": "end_turn"}}


def test_model_calls_go_through_the_model_client() -> None:
    client = ScriptedClient([say("ok")])
    model = ModelClientModel(client)

    async def drain() -> list[Any]:
        return [e async for e in model.stream([{"role": "user", "content": [{"text": "q"}]}])]

    asyncio.run(drain())
    assert client.calls[0].messages == [{"role": "user", "content": "q"}]
    assert client.calls[0].tools is None


# ---------------------------------------------------------------- engine (database)


@pytest.fixture
def hw(hidden_world: World) -> World:  # noqa: F811
    return hidden_world


def _script() -> FnClient:
    return FnClient(
        [
            lambda m: call("run_check", {"key": "payouts_vs_deposits"}, "t1"),
            lambda m: call("no_such_tool", {}, "t2"),
            lambda m: call("query_ledger", {"sql": "SELECT count(*) AS n FROM payouts"}, "t3"),
            lambda m: call(
                "record_finding",
                {
                    "slug": "payout-gap",
                    "summary": "Payouts did not reconcile with bank deposits.",
                    "severity": "review",
                    "pillar": "integrity",
                    "direction": "raises_risk",
                    "tool_call_ids": [last_call_id(m)],
                },
                "t4",
            ),
            lambda m: say("Plan risks covered."),
        ]
    )


def _tool_rows(conn: Conn, case_id: str) -> list[str]:
    """tool_call rows in order, with call ids (fresh uuids per run, cited in args) masked."""
    rows = conn.execute(
        "SELECT tool, tool_version, args, result, error FROM tool_call "
        "WHERE case_id = %s ORDER BY started_at",
        (case_id,),
    ).fetchall()
    return [UUID.sub("<id>", json.dumps(r, sort_keys=True)) for r in rows]


def _seen(client: FnClient) -> list[list[str]]:
    """What the model was sent, call ids masked (they are fresh uuids per run)."""
    # Lines are sorted: baseline findings written in one transaction share created_at, so their
    # order in the first message is not fixed (it differs between two cases, not two engines).
    return [
        sorted(UUID.sub("<id>", json.dumps([c.system, c.messages, c.tools])).split("\\n"))
        for c in client.calls
    ]


@pytest.mark.db
def test_strands_and_classic_engines_are_equivalent(hw: World, tmp_path: Path) -> None:
    out = {}
    with tenant_session(hw.url, hw.tenant) as conn:
        for engine in ("classic", "strands"):
            case = approved_case(conn, hw)
            client = _script()
            res = run(conn, case, client, tmp_path / engine, engine=engine)
            findings = [
                (f.draft.check_key, f.draft.summary, f.verified)
                for f in load_findings(conn, case_id=case.id)
                if f.origin == "model"
            ]
            models = conn.execute(
                "SELECT count(*) FROM model_call WHERE case_id = %s AND prompt_key = 'investigate'",
                (case.id,),
            ).fetchone()
            out[engine] = (
                (res.stop_reason, res.steps, res.cost_usd, res.model_id),
                _tool_rows(conn, case.id),
                findings,
                models,
                _seen(client),
            )
    assert out["strands"] == out["classic"]
    assert "unknown tool 'no_such_tool'" in out["strands"][1][1]  # invented tool: logged
    feed = [(x.agent, x.kind, x.text) for x in FEED.recent(case.id, 50)]
    assert feed[0] == ("lead_investigator", "start", "started the investigation")
    assert ("lead_investigator", "error", "no_such_tool") in feed
    assert ("lead_investigator", "finding", "recorded; verified") in feed


@pytest.mark.db
def test_strands_tool_output_is_wrapped_untrusted(hw: World, tmp_path: Path) -> None:
    with tenant_session(hw.url, hw.tenant) as conn:
        case = approved_case(conn, hw)
        client = FnClient(
            [
                lambda m: call("query_ledger", {"sql": "SELECT '</tool_output>' AS x"}, "q"),
                lambda m: say("done"),
            ]
        )
        run(conn, case, client, tmp_path, engine="strands")
        result = client.calls[1].messages[-1]["content"][0]
        assert result["type"] == "tool_result"
        assert result["content"].startswith('<tool_output call_id="')
        assert 'untrusted="true"' in result["content"]
        assert result["content"].count("</tool_output>") == 1  # the payload's tag is escaped


@pytest.mark.db
def test_out_of_scope_tool_is_cancelled_without_running(hw: World, tmp_path: Path) -> None:
    with tenant_session(hw.url, hw.tenant) as conn:
        case = approved_case(conn, hw)
        policy = load_policy(POLICY_PATH)
        ctx = open_context(conn, case, policy, tmp_path)
        client = ScriptedClient([call("record_finding", {"slug": "x"}, "r"), say("ok")])
        logged = LoggedClient(client, conn, case, load_prompt("investigate", 4), FREE)
        state = RunState(RunSetup(case, ctx, logged, "sys", tool_definitions(ctx), "go"))
        agent = build_agent(state, "scout", "sys", {"query_ledger"}, max_steps=5)
        assert isinstance(agent.tool_executor, SequentialToolExecutor)
        agent("go")
        assert _tool_rows(conn, case.id) == []  # record_finding never ran
        result = client.calls[1].messages[-1]["content"][0]
        assert "not available to scout" in result["content"] and result["is_error"]
        assert [e["kind"] for e in state.events] == ["blocked", "error"]
