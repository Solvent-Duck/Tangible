"""OIDC for the workbench API: bearer JWT access tokens verified against the IdP's JWKS.

A fake IdP is served through httpx.MockTransport (discovery and JWKS); no network.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from tangible.agent.llm import NullClient
from tangible.db.session import tenant_session
from tangible.workbench.app import create_app
from tangible.workbench.auth import ProviderError, Unauthenticated
from tangible.workbench.oidc import OidcConfig, OidcProvider
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH, World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack
from test_workbench_api import stack as stack  # noqa: F401

pytestmark = pytest.mark.db

ISSUER = "https://idp.test"
CLIENT_ID = "tangible-web"
API_AUD = "tangible-api"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeIdp:
    def __init__(self) -> None:
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.jwks_keys: dict[str, dict[str, Any]] = {}
        self.add_key("rsa1", self.rsa, "RS256")
        self.add_key("ec1", self.ec, "ES256")
        self.discovery: dict[str, Any] = {
            "issuer": ISSUER,
            "jwks_uri": f"{ISSUER}/jwks",
        }
        self.hits: dict[str, int] = {}

    def add_key(self, kid: str, private: Any, alg: str) -> None:
        pub = private.public_key()
        algo = RSAAlgorithm if alg.endswith("256") and alg != "ES256" else ECAlgorithm
        jwk = json.loads(algo.to_jwk(pub))
        jwk.update(kid=kid, use="sig", alg=alg)
        self.jwks_keys[kid] = jwk

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        self.hits[path] = self.hits.get(path, 0) + 1
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery)
        if path == "/jwks":
            return httpx.Response(200, json={"keys": list(self.jwks_keys.values())})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    @staticmethod
    def sign(claims: dict[str, Any], key: Any, alg: str, kid: str | None) -> str:
        headers = {"kid": kid} if kid else {}
        return jwt.encode(claims, key, algorithm=alg, headers=headers)

    def claims(self, tenant: str, sub: str = "ana", **over: Any) -> dict[str, Any]:
        now = int(time.time())
        c: dict[str, Any] = {
            "iss": ISSUER, "aud": API_AUD, "sub": sub, "exp": now + 300, "iat": now,
            "tangible_tenant": tenant,
        }  # fmt: skip
        c.update(over)
        return {k: v for k, v in c.items() if v is not None}

    def token(self, tenant: str, sub: str = "ana", **over: Any) -> str:
        return self.sign(self.claims(tenant, sub, **over), self.rsa, "RS256", "rsa1")


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def make_settings(w: World, tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": w.url, "evidence_dir": tmp_path / "evidence", "policy_path": POLICY_PATH,
        "client_factory": NullClient, "oidc_issuer": ISSUER, "oidc_client_id": CLIENT_ID,
        "oidc_audience": API_AUD,
    }  # fmt: skip
    base.update(over)
    return Settings(**base)


class Oidc:
    def __init__(self, idp: FakeIdp, stack: Stack, tmp_path: Path) -> None:
        self.idp, self.stack, self.w = idp, stack, stack.w
        self.app = create_app(make_settings(self.w, tmp_path), http_client=idp.client())
        self.provider: OidcProvider = self.app.state.authenticator.verifier
        self.clock = [1000.0]
        self.provider.clock = lambda: self.clock[0]
        self.http = TestClient(self.app, base_url="https://wb.test")

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def api(self, token: str) -> httpx.Response:
        return self.http.get("/api/cases", headers=self.bearer(token))


@pytest.fixture
def oidc(stack: Stack, idp: FakeIdp, tmp_path: Path) -> Oidc:  # noqa: F811
    return Oidc(idp, stack, tmp_path)


# ---------------------------------------------------------------- bearer tokens (API)


def test_valid_token_and_role_filtering(oidc: Oidc) -> None:
    case = oidc.stack.new_case("ana")
    t = oidc.w.tenant
    mine = oidc.api(oidc.idp.token(t, "ana"))
    assert mine.status_code == 200 and [c["id"] for c in mine.json()] == [case]
    assert oidc.api(oidc.idp.token(t, "bob")).json() == []  # analysts see assigned cases only
    bob = oidc.http.get(f"/api/cases/{case}", headers=oidc.bearer(oidc.idp.token(t, "bob")))
    assert bob.status_code == 403
    ana = oidc.http.get(f"/api/cases/{case}", headers=oidc.bearer(oidc.idp.token(t, "ana")))
    assert ana.status_code == 200


def test_ec_token_is_accepted(oidc: Oidc) -> None:
    tok = oidc.idp.sign(oidc.idp.claims(oidc.w.tenant), oidc.idp.ec, "ES256", "ec1")
    assert oidc.api(tok).status_code == 200


def test_role_comes_from_the_database_not_claims(oidc: Oidc) -> None:
    tok = oidc.idp.token(oidc.w.tenant, "bob", role="credit_admin", roles=["credit_admin"])
    body = {
        "borrower_id": oidc.w.borrower, "as_of": oidc.w.as_of.isoformat(), "loan_amount": "50000",
    }  # fmt: skip
    r = oidc.http.post("/api/cases", headers=oidc.bearer(tok), json=body)
    assert r.status_code == 403  # bob is an analyst in app_user: the claims are ignored


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://evil.test"},
        {"aud": "someone-else"},
        {"aud": [API_AUD, "other"]},  # multiple audiences need azp == client_id
        {"exp": int(time.time()) - 3600},
        {"nbf": int(time.time()) + 3600},
        {"iat": int(time.time()) + 3600},
        {"sub": None},
        {"tangible_tenant": None},
        {"tangible_tenant": "not-a-uuid"},
        {"tangible_tenant": str(uuid.uuid4())},
    ],
    ids=lambda o: next(iter(o)) + "=" + str(next(iter(o.values())))[:12],
)
def test_bad_claims_are_401(oidc: Oidc, over: dict[str, Any]) -> None:
    t = oidc.w.tenant
    r = oidc.api(oidc.idp.token(t, **over))
    assert r.status_code == 401, r.text
    assert oidc.api(oidc.idp.token(t)).status_code == 200  # the harness itself is fine


def test_multiple_audiences_with_matching_azp_ok(oidc: Oidc) -> None:
    tok = oidc.idp.token(oidc.w.tenant, aud=[API_AUD, "other"], azp=CLIENT_ID)
    assert oidc.api(tok).status_code == 200


def test_unknown_subject_is_401_and_not_provisioned(oidc: Oidc) -> None:
    assert oidc.api(oidc.idp.token(oidc.w.tenant, "stranger")).status_code == 401
    with tenant_session(oidc.w.url, oidc.w.tenant) as conn:
        row = conn.execute("SELECT count(*) FROM app_user WHERE subject = 'stranger'").fetchone()
    assert row and row[0] == 0


def test_alg_none_and_hs256_confusion_are_401(oidc: Oidc) -> None:
    idp = oidc.idp
    claims = idp.claims(oidc.w.tenant)
    head = b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "rsa1"}).encode())
    none_tok = f"{head}.{b64(json.dumps(claims).encode())}."
    assert oidc.api(none_tok).status_code == 401

    pem = idp.rsa.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "rsa1"}).encode())
    signing = f"{h}.{b64(json.dumps(claims).encode())}"
    sig = b64(hmac.new(pem, signing.encode(), hashlib.sha256).digest())
    assert oidc.api(f"{signing}.{sig}").status_code == 401


def test_alg_must_match_key_type(oidc: Oidc) -> None:
    # ES256 header pointing at the RSA key id (and vice versa), signed with the matching private
    # key so only the key-type check can reject it.
    c = oidc.idp.claims(oidc.w.tenant)
    assert oidc.api(oidc.idp.sign(c, oidc.idp.ec, "ES256", "rsa1")).status_code == 401
    assert oidc.api(oidc.idp.sign(c, oidc.idp.rsa, "RS256", "ec1")).status_code == 401


def test_missing_kid_and_bad_signature_are_401(oidc: Oidc) -> None:
    c = oidc.idp.claims(oidc.w.tenant)
    assert oidc.api(oidc.idp.sign(c, oidc.idp.rsa, "RS256", None)).status_code == 401
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert oidc.api(oidc.idp.sign(c, other, "RS256", "rsa1")).status_code == 401


def test_opaque_and_missing_tokens_are_401(oidc: Oidc) -> None:
    r = oidc.api("ya29.opaque-access-token")
    assert r.status_code == 401 and "JWT access tokens are required" in r.json()["detail"]
    assert oidc.http.get("/api/cases").status_code == 401
    r = oidc.http.get("/api/cases", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401


def test_errors_do_not_echo_the_token(oidc: Oidc) -> None:
    tok = oidc.idp.token(oidc.w.tenant, exp=int(time.time()) - 3600)
    r = oidc.api(tok)
    assert r.status_code == 401 and tok not in r.text and r.json()["detail"] == "invalid token"


def test_jwks_refetch_is_rate_limited(oidc: Oidc) -> None:
    idp, t = oidc.idp, oidc.w.tenant
    assert oidc.api(idp.token(t)).status_code == 200
    assert idp.hits["/jwks"] == 1

    # key rotation: the new kid is picked up by a single refetch once the window has passed
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.add_key("rsa2", rotated, "RS256")
    new_tok = idp.sign(idp.claims(t), rotated, "RS256", "rsa2")
    assert oidc.api(new_tok).status_code == 401  # within 60 s of the last fetch: no refetch
    assert idp.hits["/jwks"] == 1
    oidc.clock[0] += 61
    assert oidc.api(new_tok).status_code == 200
    assert idp.hits["/jwks"] == 2

    # attackers cannot force refetch storms: one refetch, then none for 60 s
    ghost = idp.sign(idp.claims(t), idp.rsa, "RS256", "ghost")
    oidc.clock[0] += 61
    assert oidc.api(ghost).status_code == 401
    assert idp.hits["/jwks"] == 3
    for _ in range(5):
        assert oidc.api(idp.sign(idp.claims(t), idp.rsa, "RS256", "ghost2")).status_code == 401
    assert idp.hits["/jwks"] == 3


# ---------------------------------------------------------------- discovery


def config(**over: Any) -> OidcConfig:
    base: dict[str, Any] = {
        "issuer": ISSUER, "client_id": CLIENT_ID, "audience": API_AUD,
        "tenant_claim": "tangible_tenant",
    }  # fmt: skip
    base.update(over)
    return OidcConfig(**base)


def test_discovery_issuer_mismatch_is_an_error(idp: FakeIdp) -> None:
    idp.discovery["issuer"] = "https://idp.test/"  # exact match required
    p = OidcProvider(config(), idp.client())
    with pytest.raises(ProviderError, match="issuer"):
        p.metadata()
    with pytest.raises(ProviderError):
        p.verify_jwt(idp.token(str(uuid.uuid4())), audience=API_AUD)


def test_discovery_endpoints_must_be_https(idp: FakeIdp) -> None:
    idp.discovery["jwks_uri"] = "http://idp.test/jwks"
    with pytest.raises(ProviderError):
        OidcProvider(config(), idp.client()).metadata()


def test_http_issuer_rejected_unless_test_flag(idp: FakeIdp) -> None:
    p = OidcProvider(config(issuer="http://idp.test"), idp.client())
    with pytest.raises(ProviderError, match="https"):
        p.metadata()
    assert "/.well-known/openid-configuration" not in idp.hits  # never even requested


def test_idp_outage_is_503_on_the_api(oidc: Oidc) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    oidc.provider.client = httpx.Client(transport=httpx.MockTransport(down))
    assert oidc.api(oidc.idp.token(oidc.w.tenant)).status_code == 503


def test_discovery_issuer_mismatch_surfaces_as_503(
    stack: Stack,  # noqa: F811
    idp: FakeIdp,
    tmp_path: Path,
) -> None:
    idp.discovery["issuer"] = "https://other.test"
    o = Oidc(idp, stack, tmp_path)
    assert o.api(idp.token(o.w.tenant)).status_code == 503


# ---------------------------------------------------------------- mode separation


def test_dev_routes_and_headers_are_dead_in_oidc_mode(oidc: Oidc) -> None:
    c = oidc.http
    assert c.get("/api/dev/tenants").status_code == 404
    assert c.get("/api/cases", headers=oidc.stack.h("adm")).status_code == 401


def test_dev_mode_is_unchanged(stack: Stack) -> None:  # noqa: F811
    assert stack.http.app.state.settings.auth_mode == "dev"  # type: ignore[attr-defined]
    assert stack.call("GET", "/api/cases", "ana", 200).json() == []


def test_app_refuses_to_start_without_oidc_settings(test_db_url: str, tmp_path: Path) -> None:
    ok = {
        "database_url": test_db_url, "evidence_dir": tmp_path, "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID, "oidc_audience": API_AUD,
    }  # fmt: skip
    assert create_app(Settings(**ok)) is not None
    for missing, env in (
        ("oidc_issuer", "TANGIBLE_OIDC_ISSUER"),
        ("oidc_client_id", "TANGIBLE_OIDC_CLIENT_ID"),
        ("oidc_audience", "TANGIBLE_OIDC_AUDIENCE"),
    ):
        with pytest.raises(RuntimeError, match=env):
            create_app(Settings(**{**ok, missing: None}))
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app(Settings(**{**ok, "oidc_issuer": "http://idp.test"}))


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, test_db_url: str) -> None:
    for k, v in {
        "TANGIBLE_OIDC_ISSUER": ISSUER, "TANGIBLE_OIDC_CLIENT_ID": CLIENT_ID,
        "TANGIBLE_OIDC_AUDIENCE": API_AUD,
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TANGIBLE_DEV_AUTH", raising=False)
    monkeypatch.delenv("TANGIBLE_AUTH_MODE", raising=False)
    s = Settings.from_env()
    assert s.auth_mode == "oidc" and s.oidc_tenant_claim == "tangible_tenant"
    monkeypatch.setenv("TANGIBLE_AUTH_MODE", "clerk")
    assert Settings.from_env().auth_mode == "clerk"
    monkeypatch.setenv("TANGIBLE_AUTH_MODE", "nonsense")
    with pytest.raises(ValueError, match="TANGIBLE_AUTH_MODE"):
        Settings.from_env()
    monkeypatch.setenv("TANGIBLE_AUTH_MODE", "oidc")
    monkeypatch.setenv("TANGIBLE_DEV_AUTH", "1")
    assert Settings.from_env().auth_mode == "dev"


def test_audience_must_be_set_and_differ_from_client_id(
    stack: Stack,  # noqa: F811
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="TANGIBLE_OIDC_AUDIENCE"):
        create_app(make_settings(stack.w, tmp_path, oidc_audience=None))
    with pytest.raises(RuntimeError, match="not the client id"):
        create_app(make_settings(stack.w, tmp_path, oidc_audience=CLIENT_ID))


def test_id_token_is_not_an_api_bearer(oidc: Oidc, idp: FakeIdp) -> None:
    tok = idp.token(oidc.w.tenant, aud=API_AUD, nonce="n-123")
    r = oidc.http.get("/api/cases", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_verify_jwt_is_directly_usable(idp: FakeIdp) -> None:
    p = OidcProvider(config(), idp.client())
    t = str(uuid.uuid4())
    assert p.verify_jwt(idp.token(t), audience=API_AUD)["sub"] == "ana"
    with pytest.raises(Unauthenticated):
        p.verify_jwt(idp.token(t), audience=API_AUD, nonce="n")  # id-token mode needs a nonce
    ok = idp.token(t, aud=API_AUD, nonce="n1")
    assert p.verify_jwt(ok, audience=API_AUD, nonce="n1")["nonce"] == "n1"
