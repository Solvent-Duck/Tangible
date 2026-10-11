"""Clerk mode: session JWTs verified against a fake instance's JWKS (httpx.MockTransport), the
tenant chosen from the subject's memberships, and identity-only invite acceptance."""

import dataclasses
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from tangible.agent.llm import NullClient
from tangible.workbench.app import create_app
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH
from test_onboarding import make_tenant, token_of

pytestmark = pytest.mark.db

ISSUER = "https://clerk.tangible.test"
WEB = "https://app.tangible.test"


class FakeClerk:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="ins_1", use="sig", alg="RS256")
        self.jwks = {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path == "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def token(self, subject: str, **over: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER, "sub": subject, "azp": WEB, "exp": now + 60, "iat": now, "nbf": now,
            "sid": "sess_1",
        } | over  # fmt: skip
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "ins_1"})


def settings(url: str, tmp_path: Path, **over: Any) -> Settings:
    s = Settings(
        database_url=url, evidence_dir=tmp_path / "ev", policy_path=POLICY_PATH,
        client_factory=NullClient, auth_backend="clerk", clerk_issuer=ISSUER,
        clerk_authorized_parties=(WEB,), public_url=WEB,
    )  # fmt: skip
    return dataclasses.replace(s, **over)


@pytest.fixture
def clerk() -> FakeClerk:
    return FakeClerk()


@pytest.fixture
def px() -> str:
    """A subject prefix unique to the test: the test database is shared across tests."""
    return f"u{uuid.uuid4().hex[:8]}-"


@pytest.fixture
def http(test_db_url: str, tmp_path: Path, clerk: FakeClerk) -> TestClient:
    return TestClient(create_app(settings(test_db_url, tmp_path), http_client=clerk.client()))


def bearer(token: str, tenant: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if tenant:
        h["X-Tangible-Tenant"] = tenant
    return h


def test_session_token_maps_to_membership(
    http: TestClient, clerk: FakeClerk, test_db_url: str, px: str
) -> None:
    tenant = make_tenant(test_db_url, prefix=px)

    def invites(subject: str, tenant: str | None = None, **claims: Any) -> int:
        tok = clerk.token(px + subject, **claims)
        return http.get("/api/invites", headers=bearer(tok, tenant)).status_code

    assert invites("adm") == 200
    # Analysts are members too, but cannot see invites.
    assert invites("ana") == 403
    # Explicit tenant must be one of the subject's memberships.
    assert invites("adm", tenant) == 200
    other = make_tenant(test_db_url, "Other", prefix=px)
    # adm is now in two tenants: without a header it is ambiguous; with one it resolves.
    assert invites("adm") == 401
    assert invites("adm", other) == 200
    me = http.get("/api/me", headers=bearer(clerk.token(px + "adm", email="adm@x.test"))).json()
    assert {m["tenant_id"] for m in me["memberships"]} == {tenant, other}
    assert me["email"] == "adm@x.test" and me["auth_mode"] == "clerk"


@pytest.mark.parametrize(
    "over",
    [
        {"azp": "https://evil.test"},
        {"azp": None},
        {"exp": int(time.time()) - 600},
        {"iss": "https://other.clerk.test"},
        {"sub": None},
    ],
)
def test_bad_tokens_are_rejected(
    http: TestClient, clerk: FakeClerk, test_db_url: str, over: dict[str, Any], px: str
) -> None:
    make_tenant(test_db_url, prefix=px)
    assert http.get("/api/invites", headers=bearer(clerk.token(px + "adm"))).status_code == 200
    r = http.get("/api/invites", headers=bearer(clerk.token(px + "adm", **over)))
    assert r.status_code == 401


def test_wrong_key_and_no_token(
    http: TestClient, clerk: FakeClerk, test_db_url: str, px: str
) -> None:
    make_tenant(test_db_url, prefix=px)
    forged = FakeClerk().token(px + "adm")  # same kid, different key
    assert http.get("/api/invites", headers=bearer(forged)).status_code == 401
    assert http.get("/api/invites").status_code == 401


def test_unknown_subject_has_identity_but_no_tenant(
    http: TestClient, clerk: FakeClerk, px: str
) -> None:
    tok = clerk.token(px + "new")
    assert http.get("/api/onboarding/state", headers=bearer(tok)).status_code == 401
    me = http.get("/api/me", headers=bearer(tok))
    assert me.status_code == 200 and me.json()["memberships"] == []


def test_signup_then_accept_invite(
    http: TestClient, clerk: FakeClerk, test_db_url: str, px: str
) -> None:
    tenant = make_tenant(test_db_url, prefix=px)
    sent = http.post(
        "/api/invites", headers=bearer(clerk.token(px + "adm")),
        json={"email": "o@acme.test", "business_name": "Acme"},
    )  # fmt: skip
    assert sent.status_code == 201, sent.text
    token = token_of(sent.json()["link"])
    tok = clerk.token(px + "owner", email="o@acme.test")
    acc = http.post("/api/onboarding/accept", headers=bearer(tok), json={"token": token})
    assert acc.status_code == 200 and acc.json()["tenant_id"] == tenant
    st = http.get("/api/onboarding/state", headers=bearer(tok))
    assert st.status_code == 200 and st.json()["business_name"] == "Acme"
    assert http.get("/api/cases", headers=bearer(tok)).status_code == 403


def test_clerk_mode_needs_its_settings(test_db_url: str, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="TANGIBLE_CLERK_ISSUER"):
        create_app(settings(test_db_url, tmp_path, clerk_issuer=None))
    with pytest.raises(RuntimeError, match="AUTHORIZED_PARTIES"):
        create_app(settings(test_db_url, tmp_path, clerk_authorized_parties=()))


def test_clerk_mode_does_not_serve_the_legacy_ui(http: TestClient) -> None:
    assert http.get("/dev/login").status_code == 404
    assert http.get("/healthz").status_code == 200
