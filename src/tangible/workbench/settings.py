"""Workbench configuration, read from the environment (tests construct `Settings` directly)."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from tangible.agent.llm import ModelClient, Pricing, client_from_env
from tangible.config import database_url

_DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "policies" / "default.yaml"


@dataclass(frozen=True)
class Settings:
    database_url: str
    evidence_dir: Path
    policy_path: Path = _DEFAULT_POLICY
    dev_auth: bool = False
    pricing: Pricing = field(default_factory=lambda: Pricing(Decimal(0), Decimal(0)))
    # Builds the model client for plan/investigation runs. Defaults to TANGIBLE_MODEL_BACKEND
    # (none -> NullClient), so the app runs with no model configured.
    client_factory: Callable[[], ModelClient] = client_from_env
    max_upload_bytes: int = 25 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_url=database_url(),
            evidence_dir=Path(env.get("TANGIBLE_EVIDENCE_DIR", "var/evidence")),
            policy_path=Path(env.get("TANGIBLE_POLICY_PATH", str(_DEFAULT_POLICY))),
            dev_auth=env.get("TANGIBLE_DEV_AUTH") == "1",
            pricing=Pricing(
                Decimal(env.get("TANGIBLE_PRICE_IN_PER_MTOK", "0")),
                Decimal(env.get("TANGIBLE_PRICE_OUT_PER_MTOK", "0")),
            ),
        )
