"""The only vendor-aware module. Tools and policy depend on this Protocol, not on Bedrock."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    model_id: str = ""


class ModelClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...
