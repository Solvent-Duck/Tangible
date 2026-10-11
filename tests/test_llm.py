from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tangible.agent.llm import (
    MAX_CACHE_POINTS,
    BedrockClient,
    LoggedClient,
    ModelConfigError,
    ModelResponse,
    Pricing,
    ScriptedClient,
    cost_usd,
    prompt_cache_enabled,
)
from tangible.agent.prompts import load_prompt
from tangible.db.cases import create_case
from tangible.db.repo import create_borrower
from tangible.db.session import admin_connect, tenant_session


class FakeRuntime:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.response


TOOLS = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": "hello"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "u1", "name": "t", "input": {"a": 1}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "ok", "is_error": False},
            {"type": "tool_result", "tool_use_id": "u2", "content": {"x": 1}, "is_error": True},
        ],
    },
]


def test_bedrock_request_and_response_mapping() -> None:
    fake = FakeRuntime(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "hi "},
                        {"text": "there"},
                        {"toolUse": {"toolUseId": "u9", "name": "t", "input": {"k": 2}}},
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 11, "outputTokens": 7},
        }
    )
    client = BedrockClient("model-x", "eu-west-2", client=fake)
    resp = client.complete(system="sys", messages=MESSAGES, tools=TOOLS)

    (req,) = fake.requests
    assert req["modelId"] == "model-x"
    assert req["system"] == [{"text": "sys"}]
    assert req["messages"][0] == {"role": "user", "content": [{"text": "hello"}]}
    assert req["messages"][1]["content"][1] == {
        "toolUse": {"toolUseId": "u1", "name": "t", "input": {"a": 1}}
    }
    assert req["messages"][2]["content"] == [
        {"toolResult": {"toolUseId": "u1", "content": [{"text": "ok"}], "status": "success"}},
        {"toolResult": {"toolUseId": "u2", "content": [{"json": {"x": 1}}], "status": "error"}},
    ]
    assert req["toolConfig"] == {
        "tools": [
            {
                "toolSpec": {
                    "name": "t",
                    "description": "d",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }

    assert resp == ModelResponse(
        text="hi there",
        tool_calls=({"id": "u9", "name": "t", "input": {"k": 2}},),
        model_id="model-x",
        stop_reason="tool_use",
        input_tokens=11,
        output_tokens=7,
    )


def test_bedrock_no_tools_omits_tool_config() -> None:
    fake = FakeRuntime(
        {"output": {"message": {"content": [{"text": "x"}]}}, "stopReason": "end_turn"}
    )
    BedrockClient("m", "r", client=fake).complete(system="s", messages=MESSAGES[:1])
    assert "toolConfig" not in fake.requests[0]


CLAUDE = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
CP = {"cachePoint": {"type": "default"}}


def _count_cache_points(req: dict[str, Any]) -> int:
    n = sum(1 for b in req["system"] if "cachePoint" in b)
    n += sum(1 for t in req.get("toolConfig", {}).get("tools", []) if "cachePoint" in t)
    n += sum(1 for m in req["messages"] for b in m["content"] if "cachePoint" in b)
    return n


@pytest.fixture
def no_cache_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("TANGIBLE_BEDROCK_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("TANGIBLE_PRICE_CACHE_READ_PER_MTOK", raising=False)
    monkeypatch.delenv("TANGIBLE_PRICE_CACHE_WRITE_PER_MTOK", raising=False)
    return monkeypatch


def test_cache_default_follows_model_id(no_cache_env: pytest.MonkeyPatch) -> None:
    assert BedrockClient(CLAUDE, "r").prompt_cache is True
    assert BedrockClient("anthropic.claude-opus-5-5", "r").prompt_cache is True
    assert BedrockClient("amazon.nova-pro-v1:0", "r").prompt_cache is False
    assert BedrockClient("model-x", "r").prompt_cache is False


def test_cache_env_override(no_cache_env: pytest.MonkeyPatch) -> None:
    no_cache_env.setenv("TANGIBLE_BEDROCK_PROMPT_CACHE", "0")
    assert BedrockClient(CLAUDE, "r").prompt_cache is False
    no_cache_env.setenv("TANGIBLE_BEDROCK_PROMPT_CACHE", "1")
    assert BedrockClient("amazon.nova-pro-v1:0", "r").prompt_cache is True
    no_cache_env.setenv("TANGIBLE_BEDROCK_PROMPT_CACHE", "banana")
    with pytest.raises(ModelConfigError, match="PROMPT_CACHE"):
        BedrockClient(CLAUDE, "r")
    assert prompt_cache_enabled("x", {"TANGIBLE_BEDROCK_PROMPT_CACHE": " 1 "}) is True
    # explicit constructor argument beats the environment
    assert BedrockClient(CLAUDE, "r", prompt_cache=False).prompt_cache is False


def test_request_without_cache_has_no_cache_points(no_cache_env: pytest.MonkeyPatch) -> None:
    req = BedrockClient("model-x", "r").build_request(system="s", messages=MESSAGES, tools=TOOLS)
    assert _count_cache_points(req) == 0
    off = BedrockClient(CLAUDE, "r", prompt_cache=False)
    assert _count_cache_points(off.build_request(system="s", messages=MESSAGES, tools=TOOLS)) == 0


def test_request_with_cache_places_checkpoints(no_cache_env: pytest.MonkeyPatch) -> None:
    req = BedrockClient(CLAUDE, "r").build_request(system="s", messages=MESSAGES, tools=TOOLS)
    assert req["system"] == [{"text": "s"}, CP]
    assert req["toolConfig"]["tools"][-1] == CP
    assert len(req["toolConfig"]["tools"]) == 2
    # latest message ends with a checkpoint; so does the previous user turn (MESSAGES[0])
    assert req["messages"][-1]["content"][-1] == CP
    assert req["messages"][0]["content"] == [{"text": "hello"}, CP]
    assert CP not in req["messages"][1]["content"]  # assistant turn untouched
    assert _count_cache_points(req) == 4


def test_cache_checkpoints_stay_within_limit(no_cache_env: pytest.MonkeyPatch) -> None:
    client = BedrockClient(CLAUDE, "r")
    long: list[dict[str, Any]] = []
    for i in range(30):
        long.append({"role": "user", "content": f"q{i}"})
        long.append({"role": "assistant", "content": f"a{i}"})
    long.append({"role": "user", "content": "last"})
    for tools in (TOOLS, None):
        for msgs in (long, MESSAGES[:1], [], long[:2]):
            req = client.build_request(system="s", messages=msgs, tools=tools)
            assert _count_cache_points(req) <= MAX_CACHE_POINTS
    req = client.build_request(system="s", messages=long, tools=TOOLS)
    assert _count_cache_points(req) == MAX_CACHE_POINTS
    # only the newest two user turns carry a message checkpoint
    marked = [i for i, m in enumerate(req["messages"]) if CP in m["content"]]
    assert marked == [len(long) - 3, len(long) - 1]
    # no tools: no tool checkpoint, still system + messages
    req = client.build_request(system="s", messages=long)
    assert "toolConfig" not in req
    assert _count_cache_points(req) == 3


def test_build_request_does_not_mutate_inputs(no_cache_env: pytest.MonkeyPatch) -> None:
    import copy

    msgs, tools = copy.deepcopy(MESSAGES), copy.deepcopy(TOOLS)
    BedrockClient(CLAUDE, "r").build_request(system="s", messages=msgs, tools=tools)
    assert msgs == MESSAGES and tools == TOOLS


def test_cache_does_not_change_non_cache_content(no_cache_env: pytest.MonkeyPatch) -> None:
    on = BedrockClient(CLAUDE, "r", prompt_cache=True).build_request(
        system="s", messages=MESSAGES, tools=TOOLS
    )
    off = BedrockClient(CLAUDE, "r", prompt_cache=False).build_request(
        system="s", messages=MESSAGES, tools=TOOLS
    )

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v if x != CP]
        return v

    assert strip(on) == off


def test_usage_parsing_with_and_without_cache_fields() -> None:
    base = {"output": {"message": {"content": [{"text": "x"}]}}, "stopReason": "end_turn"}
    with_cache = FakeRuntime(
        {
            **base,
            "usage": {
                "inputTokens": 28,
                "outputTokens": 5,
                "cacheReadInputTokens": 1898,
                "cacheWriteInputTokens": 40,
            },
        }
    )
    r = BedrockClient("m", "r", client=with_cache).complete(system="s", messages=MESSAGES[:1])
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (
        28, 5, 1898, 40,
    )  # fmt: skip
    without = FakeRuntime({**base, "usage": {"inputTokens": 3, "outputTokens": 4}})
    r = BedrockClient("m", "r", client=without).complete(system="s", messages=MESSAGES[:1])
    assert (r.cache_read_tokens, r.cache_write_tokens) == (0, 0)
    r = BedrockClient("m", "r", client=FakeRuntime(base)).complete(system="s", messages=[])
    assert (r.input_tokens, r.cache_read_tokens, r.cache_write_tokens) == (0, 0, 0)


def test_complete_sends_cache_points_through_converse(no_cache_env: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime({"output": {"message": {"content": [{"text": "x"}]}}})
    BedrockClient(CLAUDE, "r", client=fake).complete(system="s", messages=MESSAGES, tools=TOOLS)
    assert _count_cache_points(fake.requests[0]) == 4


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANGIBLE_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("TANGIBLE_BEDROCK_REGION", raising=False)
    with pytest.raises(ModelConfigError, match="TANGIBLE_BEDROCK_MODEL_ID"):
        BedrockClient.from_env()
    monkeypatch.setenv("TANGIBLE_BEDROCK_MODEL_ID", "m")
    with pytest.raises(ModelConfigError, match="TANGIBLE_BEDROCK_REGION"):
        BedrockClient.from_env()
    monkeypatch.setenv("TANGIBLE_BEDROCK_REGION", "r")
    c = BedrockClient.from_env()
    assert (c.model_id, c.region) == ("m", "r")


def test_scripted_client_order_records_and_exhaustion() -> None:
    a, b = ModelResponse("a"), ModelResponse("b")
    c = ScriptedClient([a, b])
    assert c.complete(system="s1", messages=[{"role": "user", "content": "1"}]) is a
    assert c.complete(system="s2", messages=[], tools=TOOLS) is b
    assert [x.system for x in c.calls] == ["s1", "s2"]
    assert c.calls[0].tools is None and c.calls[1].tools == TOOLS
    with pytest.raises(RuntimeError, match="exhausted"):
        c.complete(system="s3", messages=[])


def test_cost_usd() -> None:
    p = Pricing(Decimal("3"), Decimal("15"))
    r = ModelResponse("x", input_tokens=2_000, output_tokens=500)
    assert cost_usd(r, p) == Decimal("0.0135")


def test_cost_usd_cache_defaults_to_multiples_of_input(no_cache_env: pytest.MonkeyPatch) -> None:
    p = Pricing(Decimal("3"), Decimal("15"))
    r = ModelResponse(
        "x", input_tokens=1_000, output_tokens=0, cache_read_tokens=10_000, cache_write_tokens=2_000
    )
    # 1000*3 + 10000*0.3 + 2000*3.75 = 3000 + 3000 + 7500 per 1M
    assert cost_usd(r, p) == Decimal("0.0135")


def test_cost_usd_cache_price_overrides(no_cache_env: pytest.MonkeyPatch) -> None:
    r = ModelResponse("x", cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    p = Pricing(Decimal("3"), Decimal("15"))
    no_cache_env.setenv("TANGIBLE_PRICE_CACHE_READ_PER_MTOK", "0.5")
    no_cache_env.setenv("TANGIBLE_PRICE_CACHE_WRITE_PER_MTOK", "4")
    assert cost_usd(r, p) == Decimal("4.5")
    # an explicit field beats the environment
    explicit = Pricing(Decimal("3"), Decimal("15"), Decimal("1"), Decimal("2"))
    assert cost_usd(r, explicit) == Decimal("3")
    no_cache_env.setenv("TANGIBLE_PRICE_CACHE_READ_PER_MTOK", "abc")
    with pytest.raises(ModelConfigError, match="CACHE_READ"):
        cost_usd(r, p)


def test_logged_client_totals_include_cache_tokens(no_cache_env: pytest.MonkeyPatch) -> None:
    # no DB: exercise the accumulation with a stub connection
    class Stub:
        def __init__(self) -> None:
            self.rows: list[tuple[Any, ...]] = []

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            self.rows.append(params)

    @dataclass
    class C:
        tenant_id: str = "t"
        id: str = "c"

    inner = ScriptedClient(
        [
            ModelResponse(
                "a", model_id="m", input_tokens=10, output_tokens=5, cache_write_tokens=100
            ),
            ModelResponse(
                "b", model_id="m", input_tokens=10, output_tokens=5, cache_read_tokens=100
            ),
        ]
    )
    prompt = load_prompt("system", 1)
    stub = Stub()
    logged = LoggedClient(inner, stub, C(), prompt, Pricing(Decimal("3"), Decimal("15")))  # type: ignore[arg-type]
    logged.complete(system="s", messages=[])
    logged.complete(system="s", messages=[])
    assert (logged.cache_read_tokens, logged.cache_write_tokens) == (100, 100)
    assert logged.total_tokens == 230
    # (10*3 + 5*15 + 100*3.75)/1e6 + (10*3 + 5*15 + 100*0.3)/1e6
    assert logged.cost == Decimal("0.000480") + Decimal("0.000135")
    assert [r[-1] for r in stub.rows] == [Decimal("0.000480"), Decimal("0.000135")]


@pytest.mark.db
def test_logged_client_writes_model_call(test_db_url: str) -> None:
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('t') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    prompt = load_prompt("system", 1)
    pricing = Pricing(Decimal("3"), Decimal("15"))
    inner = ScriptedClient(
        [
            ModelResponse("a", model_id="m1", input_tokens=2_000, output_tokens=500),
            ModelResponse("b", model_id="m1", input_tokens=1_000, output_tokens=100),
        ]
    )
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        case = create_case(conn, tenant, borrower, datetime(2010, 7, 1, tzinfo=UTC))
        logged = LoggedClient(inner, conn, case, prompt, pricing)
        logged.complete(system="s", messages=[])
        logged.complete(system="s", messages=[])
        rows = conn.execute(
            "SELECT tenant_id, case_id, model_id, prompt_key, prompt_version, prompt_sha256, "
            "input_tokens, output_tokens, cost_usd FROM model_call ORDER BY input_tokens DESC"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0] == (
        type(rows[0][0])(tenant), type(rows[0][1])(case.id), "m1", "system", "1",
        prompt.sha256, 2_000, 500, Decimal("0.013500"),
    )  # fmt: skip
    assert rows[1][8] == Decimal("0.004500")
    assert logged.calls == 2
    assert logged.total_tokens == 3_600
    assert logged.cost == Decimal("0.0180")
