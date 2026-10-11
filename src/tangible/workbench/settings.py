"""Workbench configuration, read from the environment (tests construct `Settings` directly)."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from tangible.agent.llm import ModelClient, Pricing, client_from_env
from tangible.config import database_url
from tangible.secrets import EnvSecrets, SecretStore
from tangible.web.fixtures import demo_provider
from tangible.web.provider import WebProvider

_DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "policies" / "default.yaml"


def _opt_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() not in ("0", "false", "no", "off")


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
    # Web lookup for the agent: "none" (the tool reports it is not available) or "fixtures"
    # (canned pages, no network). There is deliberately no live provider here.
    web_provider: str = "none"
    # With web_provider == "fixtures": a path to an index.json; None means the packaged demo set.
    web_fixtures: Path | None = None
    # --- OIDC (auth_mode == "oidc"). One IdP per deployment is assumed.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    # A SecretStore ref (e.g. `env:OIDC_SECRET`), never the secret itself.
    oidc_client_secret_ref: str | None = None
    oidc_audience: str | None = None  # API token audience; defaults to the client id
    oidc_redirect_uri: str | None = None
    oidc_scopes: str = "openid email profile"
    oidc_tenant_claim: str = "tangible_tenant"
    # HMAC key for session cookies (>= 32 bytes). Excluded from repr so it never reaches logs.
    session_secret: str | None = field(default=None, repr=False)
    session_hours: float = 8.0
    cookie_secure: bool | None = None  # None: on in oidc mode, off in dev mode
    # Test-only: permit http:// issuer/endpoints. Deliberately not readable from the environment.
    oidc_allow_insecure_http: bool = False
    secret_store: SecretStore = field(default_factory=EnvSecrets, repr=False)

    @property
    def auth_mode(self) -> str:
        """ "dev" only when TANGIBLE_DEV_AUTH=1 (dev_auth); otherwise "oidc"."""
        return "dev" if self.dev_auth else "oidc"

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else not self.dev_auth

    def build_web_provider(self) -> WebProvider | None:
        """The provider handed to the agent's tools; None when web lookup is off."""
        mode = self.web_provider.strip().lower()
        if mode == "none":
            return None
        if mode == "fixtures":
            return demo_provider(self.web_fixtures)
        raise ValueError(f"TANGIBLE_WEB_PROVIDER must be 'none' or 'fixtures', not {mode!r}")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        settings = cls(
            database_url=database_url(),
            evidence_dir=Path(env.get("TANGIBLE_EVIDENCE_DIR", "var/evidence")),
            policy_path=Path(env.get("TANGIBLE_POLICY_PATH", str(_DEFAULT_POLICY))),
            web_provider=env.get("TANGIBLE_WEB_PROVIDER") or "none",
            web_fixtures=Path(p) if (p := env.get("TANGIBLE_WEB_FIXTURES")) else None,
            dev_auth=env.get("TANGIBLE_DEV_AUTH") == "1",
            oidc_issuer=env.get("TANGIBLE_OIDC_ISSUER") or None,
            oidc_client_id=env.get("TANGIBLE_OIDC_CLIENT_ID") or None,
            oidc_client_secret_ref=env.get("TANGIBLE_OIDC_CLIENT_SECRET_REF") or None,
            oidc_audience=env.get("TANGIBLE_OIDC_AUDIENCE") or None,
            oidc_redirect_uri=env.get("TANGIBLE_OIDC_REDIRECT_URI") or None,
            oidc_scopes=env.get("TANGIBLE_OIDC_SCOPES") or "openid email profile",
            oidc_tenant_claim=env.get("TANGIBLE_OIDC_TENANT_CLAIM") or "tangible_tenant",
            session_secret=env.get("TANGIBLE_SESSION_SECRET") or None,
            session_hours=float(env.get("TANGIBLE_SESSION_HOURS", "8")),
            cookie_secure=_opt_bool(env.get("TANGIBLE_COOKIE_SECURE")),
            pricing=Pricing(
                Decimal(env.get("TANGIBLE_PRICE_IN_PER_MTOK", "0")),
                Decimal(env.get("TANGIBLE_PRICE_OUT_PER_MTOK", "0")),
            ),
        )
        settings.build_web_provider()  # fail at startup on a bad TANGIBLE_WEB_* setting
        return settings
