"""The only vendor-aware module. Tools and policy depend on this Protocol, not on Bedrock.

Vendor-neutral message shape (what the agent loop builds and every client accepts):

    {"role": "user" | "assistant", "content": str | list[block]}

where a block is one of
    {"type": "text", "text": str}
    {"type": "tool_use", "id": str, "name": str, "input": dict}          (assistant)
    {"type": "tool_result", "tool_use_id": str, "content": str | Any,
     "is_error": bool}                                                   (user)

Tool definitions are `{"name", "description", "input_schema"}` (JSON Schema). Tool calls in a
`ModelResponse` are `{"id", "name", "input"}` dicts.
"""

import os
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from tangible.agent.prompts import Prompt
from tangible.db.cases import Case
from tangible.db.session import Conn


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    model_id: str = ""
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # Bedrock reports `inputTokens` as the NON-cached remainder; cache traffic is separate.
    # Total input tokens processed = input_tokens + cache_read_tokens + cache_write_tokens.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ModelClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


class ModelConfigError(RuntimeError):
    """Required model configuration is missing."""


# ---------------------------------------------------------------- Bedrock


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _to_converse_block(b: dict[str, Any]) -> dict[str, Any]:
    kind = b["type"]
    if kind == "text":
        return {"text": b["text"]}
    if kind == "tool_use":
        return {"toolUse": {"toolUseId": b["id"], "name": b["name"], "input": b["input"]}}
    if kind == "tool_result":
        c = b["content"]
        inner = {"text": c} if isinstance(c, str) else {"json": c}
        return {
            "toolResult": {
                "toolUseId": b["tool_use_id"],
                "content": [inner],
                "status": "error" if b.get("is_error") else "success",
            }
        }
    raise ValueError(f"unknown content block type {kind!r}")


# Bedrock allows at most 4 cache checkpoints per request for Claude models
# (https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html).
MAX_CACHE_POINTS = 4
_CACHE_POINT: dict[str, Any] = {"cachePoint": {"type": "default"}}  # default 5-minute TTL
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def prompt_cache_enabled(model_id: str, environ: Mapping[str, str] | None = None) -> bool:
    """TANGIBLE_BEDROCK_PROMPT_CACHE: "1"/"0" overrides; unset or empty means on for Anthropic
    Claude model ids (incl. inference-profile ids like `us.anthropic.claude-...`), off otherwise."""
    env = os.environ if environ is None else environ
    raw = env.get("TANGIBLE_BEDROCK_PROMPT_CACHE", "").strip().lower()
    if not raw:
        return "anthropic.claude" in model_id
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ModelConfigError(f"TANGIBLE_BEDROCK_PROMPT_CACHE must be 0 or 1, got {raw!r}")


def _with_cache_points(req: dict[str, Any]) -> None:
    """Add explicit Converse cache checkpoints, in place. At most MAX_CACHE_POINTS.

    Checkpoints are processed tools -> system -> messages, and everything before one is the cached
    prefix. We place: (1) end of the tool list, (2) end of the system prompt, (3) end of the latest
    message so the growing history is cached incrementally, (4) end of the previous user turn, so
    the prior turn's entry is still found when a turn adds more than the ~20-block lookback
    (many parallel tool results). A checkpoint under the model minimum is a harmless no-op.
    """
    budget = MAX_CACHE_POINTS
    tools = req.get("toolConfig", {}).get("tools")
    if tools and budget:
        tools.append(dict(_CACHE_POINT))
        budget -= 1
    if budget:
        req["system"].append(dict(_CACHE_POINT))
        budget -= 1
    msgs: list[dict[str, Any]] = req["messages"]
    targets = [len(msgs) - 1] if msgs else []
    prev_user = [i for i in range(len(msgs) - 1) if msgs[i]["role"] == "user"]
    if prev_user:
        targets.append(prev_user[-1])
    for i in targets[:budget]:
        if msgs[i]["content"]:
            msgs[i]["content"].append(dict(_CACHE_POINT))


class BedrockClient:
    def __init__(
        self,
        model_id: str,
        region: str,
        client: Any = None,
        prompt_cache: bool | None = None,
    ) -> None:
        self.model_id = model_id
        self.region = region
        self._client = client
        self.prompt_cache = prompt_cache_enabled(model_id) if prompt_cache is None else prompt_cache

    @classmethod
    def from_env(cls) -> "BedrockClient":
        model_id = os.environ.get("TANGIBLE_BEDROCK_MODEL_ID", "")
        region = os.environ.get("TANGIBLE_BEDROCK_REGION", "")
        missing = [
            n
            for n, v in (
                ("TANGIBLE_BEDROCK_MODEL_ID", model_id),
                ("TANGIBLE_BEDROCK_REGION", region),
            )
            if not v
        ]
        if missing:
            raise ModelConfigError(f"set {', '.join(missing)} to use the Bedrock client")
        return cls(model_id, region)

    def _runtime(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def build_request(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": [
                {
                    "role": m["role"],
                    "content": [_to_converse_block(b) for b in _blocks(m["content"])],
                }
                for m in messages
            ],
        }
        if tools:
            req["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": {"json": t["input_schema"]},
                        }
                    }
                    for t in tools
                ]
            }
        if self.prompt_cache:
            _with_cache_points(req)
        return req

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        resp = self._runtime().converse(
            **self.build_request(system=system, messages=messages, tools=tools)
        )
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in resp.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                texts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append({"id": tu["toolUseId"], "name": tu["name"], "input": tu["input"]})
        usage = resp.get("usage", {})
        return ModelResponse(
            text="".join(texts),
            tool_calls=tuple(calls),
            model_id=self.model_id,
            stop_reason=resp.get("stopReason", ""),
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            cache_read_tokens=int(usage.get("cacheReadInputTokens") or 0),
            cache_write_tokens=int(usage.get("cacheWriteInputTokens") or 0),
        )


# ---------------------------------------------------------------- null client and selection


class NullClient:
    """Used when no model is configured: ends every turn at once, makes no tool calls."""

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        return ModelResponse(text="No model configured.", stop_reason="end_turn")


def client_from_env() -> ModelClient:
    """TANGIBLE_MODEL_BACKEND = none (default) | bedrock."""
    backend = os.environ.get("TANGIBLE_MODEL_BACKEND", "none").strip().lower() or "none"
    if backend == "none":
        return NullClient()
    if backend == "bedrock":
        return BedrockClient.from_env()
    raise ModelConfigError(f"unknown TANGIBLE_MODEL_BACKEND {backend!r} (use none or bedrock)")


# ---------------------------------------------------------------- scripted (tests, dry runs)


@dataclass(frozen=True)
class RecordedCall:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None


class ScriptedClient:
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._queue = list(responses)
        self.calls: list[RecordedCall] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        self.calls.append(RecordedCall(system, list(messages), tools))
        if not self._queue:
            raise RuntimeError("ScriptedClient exhausted: no more queued responses")
        return self._queue.pop(0)


# ---------------------------------------------------------------- cost and logging


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. Supplied by configuration; never hard-coded here."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    # Unset: TANGIBLE_PRICE_CACHE_READ_PER_MTOK / ..._CACHE_WRITE_PER_MTOK if set, else the
    # Anthropic-on-Bedrock multiples of the input price (read 0.1x, 5-minute write 1.25x).
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None

    def cache_read_rate(self) -> Decimal:
        return self._cache_rate(
            self.cache_read_per_mtok, "TANGIBLE_PRICE_CACHE_READ_PER_MTOK", Decimal("0.1")
        )

    def cache_write_rate(self) -> Decimal:
        return self._cache_rate(
            self.cache_write_per_mtok, "TANGIBLE_PRICE_CACHE_WRITE_PER_MTOK", Decimal("1.25")
        )

    def _cache_rate(self, explicit: Decimal | None, env_name: str, multiple: Decimal) -> Decimal:
        if explicit is not None:
            return explicit
        raw = os.environ.get(env_name, "").strip()
        if raw:
            try:
                return Decimal(raw)
            except ArithmeticError as e:
                raise ModelConfigError(f"{env_name} must be a number, got {raw!r}") from e
        return self.input_per_mtok * multiple


def cost_usd(response: ModelResponse, pricing: Pricing) -> Decimal:
    return (
        Decimal(response.input_tokens) * pricing.input_per_mtok
        + Decimal(response.output_tokens) * pricing.output_per_mtok
        + Decimal(response.cache_read_tokens) * pricing.cache_read_rate()
        + Decimal(response.cache_write_tokens) * pricing.cache_write_rate()
    ) / Decimal(1_000_000)


@dataclass
class LoggedClient:
    """Wraps a client; records one `model_call` row per call and keeps running totals."""

    inner: ModelClient
    conn: Conn
    case: Case
    prompt: Prompt
    pricing: Pricing
    calls: int = field(default=0, init=False)
    input_tokens: int = field(default=0, init=False)
    output_tokens: int = field(default=0, init=False)
    cache_read_tokens: int = field(default=0, init=False)
    cache_write_tokens: int = field(default=0, init=False)
    cost: Decimal = field(default=Decimal(0), init=False)
    # Held around the DB write and totals only, never the model call: parallel agents (one
    # connection) wait for each other's inserts, not each other's inference.
    lock: AbstractContextManager[Any] = field(default_factory=nullcontext, repr=False)

    @property
    def total_tokens(self) -> int:
        """All tokens processed: uncached + cache-read + cache-write input, plus output."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        resp = self.inner.complete(system=system, messages=messages, tools=tools)
        with self.lock:
            self._record(resp)
        return resp

    def _record(self, resp: ModelResponse) -> None:
        cost = cost_usd(resp, self.pricing)
        self.conn.execute(
            "INSERT INTO model_call (tenant_id, case_id, model_id, prompt_key, prompt_version, "
            "prompt_sha256, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self.case.tenant_id,
                self.case.id,
                resp.model_id,
                self.prompt.key,
                str(self.prompt.version),
                self.prompt.sha256,
                resp.input_tokens,
                resp.output_tokens,
                resp.cache_read_tokens,
                resp.cache_write_tokens,
                cost.quantize(Decimal("0.000001")),
            ),
        )
        self.calls += 1
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self.cache_read_tokens += resp.cache_read_tokens
        self.cache_write_tokens += resp.cache_write_tokens
        self.cost += cost
