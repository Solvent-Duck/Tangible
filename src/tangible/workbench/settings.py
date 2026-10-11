"""Workbench configuration, read from the environment (tests construct `Settings` directly)."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from tangible.agent.llm import ModelClient, Pricing, client_from_env
from tangible.config import database_url
from tangible.web.fixtures import demo_provider
from tangible.web.provider import WebProvider

_DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "policies" / "default.yaml"


def _auth_backend(value: str | None) -> str:
    """TANGIBLE_AUTH_MODE: oidc (default) or clerk. "dev" is TANGIBLE_DEV_AUTH=1, kept as the one
    switch for the unsafe mode so it is never turned on by a typo here."""
    mode = (value or "oidc").strip().lower()
    if mode not in ("oidc", "clerk"):
        raise ValueError(f"TANGIBLE_AUTH_MODE must be 'oidc' or 'clerk', not {mode!r}")
    return mode


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
    # --- OIDC (auth_mode == "oidc"): bearer access tokens for the API. One IdP per deployment.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None  # checked against `azp` on multi-audience tokens
    oidc_audience: str | None = None  # the API's own audience (never the client id)
    oidc_tenant_claim: str = "tangible_tenant"
    # Test-only: permit http:// issuer/endpoints. Deliberately not readable from the environment.
    oidc_allow_insecure_http: bool = False
    # Hosted dev-auth demos only: one shared password (HTTP Basic) in front of everything.
    access_password: str | None = field(default=None, repr=False)
    # "oidc" or "clerk" (TANGIBLE_AUTH_MODE). dev_auth (TANGIBLE_DEV_AUTH=1) overrides both.
    auth_backend: str = "oidc"
    # --- Clerk (auth_mode == "clerk"): the instance's Frontend API URL (the token issuer) and the
    # origins allowed in a session token's `azp` (the web app's URLs).
    clerk_issuer: str | None = None
    clerk_authorized_parties: tuple[str, ...] = ()
    # --- Onboarding: where invite links point (the web app), invite lifetime, optional SMTP.
    public_url: str = "http://localhost:3000"
    invite_days: int = 14
    smtp_url: str | None = None
    # Requests per minute per client IP on the public invite routes (preview, accept).
    public_rate_per_minute: int = 30

    @property
    def auth_mode(self) -> str:
        """ "dev" when TANGIBLE_DEV_AUTH=1 (dev_auth); otherwise `auth_backend` (oidc or clerk)."""
        return "dev" if self.dev_auth else self.auth_backend

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
            evidence_dir=Path(env.get("TANGIBLE_EVIDENCE_DIR", ".evidence")),
            policy_path=Path(env.get("TANGIBLE_POLICY_PATH", str(_DEFAULT_POLICY))),
            web_provider=env.get("TANGIBLE_WEB_PROVIDER") or "none",
            web_fixtures=Path(p) if (p := env.get("TANGIBLE_WEB_FIXTURES")) else None,
            dev_auth=env.get("TANGIBLE_DEV_AUTH") == "1",
            oidc_issuer=env.get("TANGIBLE_OIDC_ISSUER") or None,
            oidc_client_id=env.get("TANGIBLE_OIDC_CLIENT_ID") or None,
            oidc_audience=env.get("TANGIBLE_OIDC_AUDIENCE") or None,
            oidc_tenant_claim=env.get("TANGIBLE_OIDC_TENANT_CLAIM") or "tangible_tenant",
            access_password=env.get("TANGIBLE_ACCESS_PASSWORD") or None,
            auth_backend=_auth_backend(env.get("TANGIBLE_AUTH_MODE")),
            clerk_issuer=env.get("TANGIBLE_CLERK_ISSUER") or None,
            clerk_authorized_parties=tuple(
                o.strip().rstrip("/")
                for o in (env.get("TANGIBLE_CLERK_AUTHORIZED_PARTIES") or "").split(",")
                if o.strip()
            ),
            public_url=env.get("TANGIBLE_PUBLIC_URL") or "http://localhost:3000",
            invite_days=int(env.get("TANGIBLE_INVITE_DAYS", "14")),
            smtp_url=env.get("TANGIBLE_SMTP_URL") or None,
            public_rate_per_minute=int(env.get("TANGIBLE_PUBLIC_RATE_PER_MINUTE", "30")),
            pricing=Pricing(
                Decimal(env.get("TANGIBLE_PRICE_IN_PER_MTOK", "0")),
                Decimal(env.get("TANGIBLE_PRICE_OUT_PER_MTOK", "0")),
            ),
        )
        settings.build_web_provider()  # fail at startup on a bad TANGIBLE_WEB_* setting
        return settings
