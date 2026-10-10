from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tangible.agent.llm import (
    BedrockClient,
    LoggedClient,
    ModelConfigError,
    ModelResponse,
    Pricing,
    ScriptedClient,
    cost_usd,
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
