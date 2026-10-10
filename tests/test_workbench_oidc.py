"""OIDC for the workbench: bearer JWTs for the API, authorization-code login for the UI.

A fake IdP is served through httpx.MockTransport (discovery, JWKS, token endpoint); no network.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
from tangible.workbench.oidc import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    OidcConfig,
    OidcProvider,
    SignedCookies,
    safe_next,
)
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH, World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack
from test_workbench_api import stack as stack  # noqa: F401

pytestmark = pytest.mark.db

ISSUER = "https://idp.test"
CLIENT_ID = "tangible-web"
API_AUD = "tangible-api"
REDIRECT = "https://wb.test/auth/callback"
SESSION_SECRET = "s" * 40
CLIENT_SECRET = "very-secret-client-value"


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
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "end_session_endpoint": f"{ISSUER}/logout",
        }
        self.hits: dict[str, int] = {}
        self.id_claims: dict[str, Any] = {}
        self.token_forms: list[dict[str, str]] = []
        self.token_auth: list[str] = []
        self.challenge = ""

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
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            self.token_auth.append(request.headers.get("Authorization", ""))
            want = b64(hashlib.sha256(form["code_verifier"].encode()).digest())
            if form.get("code") != "good-code" or want != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            tok = self.sign(self.id_claims, self.rsa, "RS256", "rsa1")
            return httpx.Response(200, json={"id_token": tok, "access_token": "opaque"})
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
        "oidc_audience": API_AUD, "oidc_redirect_uri": REDIRECT,
        "oidc_client_secret_ref": "env:OIDC_TEST_SECRET", "session_secret": SESSION_SECRET,
    }  # fmt: skip
    base.update(over)
    return Settings(**base)


class Oidc:
    def __init__(self, idp: FakeIdp, stack: Stack, tmp_path: Path) -> None:
        self.idp, self.stack, self.w = idp, stack, stack.w
        self.app = create_app(make_settings(self.w, tmp_path), http_client=idp.client())
        self.clock = [1000.0]
        self.app.state.oidc.provider.clock = lambda: self.clock[0]
        self.http = TestClient(self.app, base_url="https://wb.test")
        self.cookies = SignedCookies(SESSION_SECRET.encode())

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def api(self, token: str) -> httpx.Response:
        return self.http.get("/api/cases", headers=self.bearer(token))

    def login(self, next_: str | None = None, **over: Any) -> httpx.Response:
        """Drive /auth/login then /auth/callback; returns the callback response."""
        c = self.http
        r = c.get("/auth/login", params={"next": next_} if next_ else {}, follow_redirects=False)
        assert r.status_code == 303
        q = parse_qs(urlsplit(r.headers["location"]).query)
        self.idp.challenge = q["code_challenge"][0]
        over = {"aud": CLIENT_ID, "nonce": q["nonce"][0], **over}
        claims = self.idp.claims(self.w.tenant, **over)
        self.idp.id_claims = claims
        return c.get(
            "/auth/callback", params={"code": "good-code", "state": q["state"][0]},
            follow_redirects=False,
        )  # fmt: skip


@pytest.fixture
def oidc(
    stack: Stack,  # noqa: F811
    idp: FakeIdp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Oidc:
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
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
        "issuer": ISSUER, "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "audience": API_AUD,
        "scopes": "openid", "tenant_claim": "tangible_tenant",
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

    oidc.app.state.oidc.provider.client = httpx.Client(transport=httpx.MockTransport(down))
    assert oidc.api(oidc.idp.token(oidc.w.tenant)).status_code == 503


def test_discovery_issuer_mismatch_surfaces_as_503(
    stack: Stack,  # noqa: F811
    idp: FakeIdp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
    idp.discovery["issuer"] = "https://other.test"
    o = Oidc(idp, stack, tmp_path)
    assert o.api(idp.token(o.w.tenant)).status_code == 503
    assert o.http.get("/auth/login", follow_redirects=False).status_code == 503


# ---------------------------------------------------------------- UI login


def test_unauthenticated_ui_redirects_to_login_with_next(oidc: Oidc) -> None:
    r = oidc.http.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=%2F"
    r = oidc.http.get("/cases/new", follow_redirects=False)
    assert r.headers["location"] == "/auth/login?next=%2Fcases%2Fnew"
    r = oidc.http.post("/cases/new", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    r = oidc.http.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/auth/login"


def test_login_page_has_the_sso_button(oidc: Oidc) -> None:
    page = oidc.http.get("/login?next=/cases/new").text
    assert "Sign in with your organisation" in page and 'value="/cases/new"' in page
    assert "Dev sign-in" not in page


def test_login_redirect_parameters_and_pkce(oidc: Oidc) -> None:
    r = oidc.http.get("/auth/login", follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/authorize"
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == REDIRECT and q["scope"] == "openid email profile"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and q["state"] != q["nonce"]

    raw = oidc.http.cookies[LOGIN_COOKIE]
    data = oidc.cookies.loads("login", raw)
    assert data and data["state"] == q["state"] and data["nonce"] == q["nonce"]
    assert q["code_challenge"] == b64(hashlib.sha256(data["v"].encode()).digest())
    assert 43 <= len(data["v"]) <= 128
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc and "max-age=600" in sc


def test_login_state_differs_every_time(oidc: Oidc) -> None:
    def state() -> str:
        r = oidc.http.get("/auth/login", follow_redirects=False)
        return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]

    assert state() != state()


def test_callback_success_sets_session_and_honours_relative_next(oidc: Oidc) -> None:
    r = oidc.login(next_="/cases/new")
    assert r.status_code == 303 and r.headers["location"] == "/cases/new"
    sets = [v.lower() for k, v in r.headers.multi_items() if k == "set-cookie"]
    sess = next(v for v in sets if v.startswith(SESSION_COOKIE + "="))
    assert "httponly" in sess and "secure" in sess and "samesite=lax" in sess and "path=/" in sess
    assert "max-age=28800" in sess  # 8 hours by default
    assert any(v.startswith(LOGIN_COOKIE + "=") and "max-age=0" in v for v in sets)

    # token endpoint got client_secret_basic + the PKCE verifier + the exact redirect_uri
    form, auth = oidc.idp.token_forms[-1], oidc.idp.token_auth[-1]
    assert auth == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert form["grant_type"] == "authorization_code" and form["redirect_uri"] == REDIRECT
    assert CLIENT_SECRET not in json.dumps(form)

    page = oidc.http.get("/", follow_redirects=False)
    assert page.status_code == 200 and "ana (analyst)" in page.text
    payload = oidc.cookies.loads("session", oidc.http.cookies[SESSION_COOKIE])
    assert payload and set(payload) >= {"t", "s", "sid", "iat", "exp"}
    assert "role" not in payload and payload["exp"] - payload["iat"] == 8 * 3600


@pytest.mark.parametrize(
    "bad", ["https://evil.example/x", "//evil.example", "/\\evil.example", "javascript:alert(1)",
            "/auth/login", "evil", "/x\r\nSet-Cookie: a=b"],
)  # fmt: skip
def test_open_redirects_are_ignored(oidc: Oidc, bad: str) -> None:
    assert safe_next(bad) is None
    r = oidc.login(next_=bad)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_safe_next_accepts_same_site_paths() -> None:
    assert safe_next("/cases/1?tab=a#x") == "/cases/1?tab=a#x"
    assert safe_next(None) is None and safe_next("") is None


def test_callback_wrong_or_missing_state_is_400(oidc: Oidc) -> None:
    c = oidc.http
    c.get("/auth/login", follow_redirects=False)
    r = c.get("/auth/callback", params={"code": "good-code", "state": "wrong"})
    assert r.status_code == 400 and SESSION_COOKIE not in c.cookies
    assert c.get("/auth/callback", params={"code": "good-code"}).status_code == 400
    c.cookies.clear()
    assert c.get("/auth/callback", params={"code": "x", "state": "y"}).status_code == 400
    assert oidc.idp.token_forms == []  # never reached the token endpoint


def test_callback_tampered_login_cookie_is_400(oidc: Oidc) -> None:
    r = oidc.http.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    forged = SignedCookies(b"x" * 40).dumps("login", {"state": state, "nonce": "n", "v": "v"}, 600)
    oidc.http.cookies.set(LOGIN_COOKIE, forged, domain="wb.test")
    assert oidc.http.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400


def test_callback_nonce_mismatch_is_401(oidc: Oidc) -> None:
    r = oidc.login(nonce="attacker-chosen")
    assert r.status_code == 401 and SESSION_COOKIE not in oidc.http.cookies


def test_callback_rejects_wrong_audience_unknown_user_and_idp_error(oidc: Oidc) -> None:
    assert oidc.login(aud="another-client").status_code == 401
    assert oidc.login(sub="stranger").status_code == 401
    assert oidc.http.get("/auth/callback", params={"error": "access_denied"}).status_code == 401
    r = oidc.http.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    r = oidc.http.get("/auth/callback", params={"code": "bad", "state": q["state"][0]})
    assert r.status_code == 401  # IdP rejected the code
    r = oidc.http.get(
        "/auth/callback", params={"code": "good-code", "state": q["state"][0], "iss": "https://x"}
    )
    assert r.status_code == 400  # RFC 9207 issuer mix-up


def test_tampered_session_cookie_redirects_to_login(oidc: Oidc) -> None:
    oidc.login()
    good = oidc.http.cookies[SESSION_COOKIE]
    assert oidc.http.get("/", follow_redirects=False).status_code == 200
    body, sig = good.split(".")
    for bad in (f"{body}.{sig[:-2]}AA", f"{b64(b'{}')}.{sig}", "garbage", ""):
        oidc.http.cookies.set(SESSION_COOKIE, bad, domain="wb.test")
        r = oidc.http.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


def test_expired_session_cookie_redirects_to_login(oidc: Oidc) -> None:
    past = SignedCookies(SESSION_SECRET.encode(), now=lambda: time.time() - 100_000)
    expired = past.dumps("session", {"t": oidc.w.tenant, "s": "ana", "sid": "x"}, 3600)
    oidc.http.cookies.set(SESSION_COOKIE, expired, domain="wb.test")
    r = oidc.http.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    fresh = SignedCookies(SESSION_SECRET.encode()).dumps(
        "session", {"t": oidc.w.tenant, "s": "ana", "sid": "x"}, 3600
    )
    oidc.http.cookies.set(SESSION_COOKIE, fresh, domain="wb.test")
    assert oidc.http.get("/", follow_redirects=False).status_code == 200
    # a login cookie is not a session cookie (purpose separation)
    login_like = SignedCookies(SESSION_SECRET.encode()).dumps(
        "login", {"t": oidc.w.tenant, "s": "ana"}, 3600
    )
    oidc.http.cookies.set(SESSION_COOKIE, login_like, domain="wb.test")
    assert oidc.http.get("/", follow_redirects=False).status_code == 303


def test_removed_user_is_logged_out_on_next_request(oidc: Oidc) -> None:
    oidc.login()
    assert oidc.http.get("/", follow_redirects=False).status_code == 200
    with tenant_session(oidc.w.url, oidc.w.tenant) as conn:
        conn.execute("DELETE FROM app_user WHERE subject = 'ana'")
    r = oidc.http.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


def test_role_change_applies_immediately(oidc: Oidc) -> None:
    oidc.login()
    assert "ana (analyst)" in oidc.http.get("/").text
    with tenant_session(oidc.w.url, oidc.w.tenant) as conn:
        conn.execute("UPDATE app_user SET role = 'credit_admin' WHERE subject = 'ana'")
    assert "ana (credit_admin)" in oidc.http.get("/").text


def test_logout_clears_session_and_uses_end_session_endpoint(oidc: Oidc) -> None:
    oidc.login()
    r = oidc.http.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/logout"
    q = parse_qs(loc.query)
    assert q["post_logout_redirect_uri"] == ["https://wb.test/"] and "id_token_hint" not in q
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert SESSION_COOKIE not in oidc.http.cookies
    assert oidc.http.get("/", follow_redirects=False).status_code == 303


def test_logout_without_end_session_endpoint_goes_to_login(oidc: Oidc) -> None:
    del oidc.idp.discovery["end_session_endpoint"]
    oidc.login()
    r = oidc.http.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"


# ---------------------------------------------------------------- mode separation


def test_dev_routes_and_headers_are_dead_in_oidc_mode(oidc: Oidc) -> None:
    c = oidc.http
    assert c.get("/dev/login").status_code == 404
    assert c.post("/dev/login", data={"tenant": oidc.w.tenant, "subject": "ana"}).status_code == 404
    assert c.post("/dev/logout").status_code == 404
    h = oidc.stack.h("adm")
    assert c.get("/api/cases", headers=h).status_code == 401
    r = c.get("/", headers=h, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    # a dev-style cookie is not honoured either
    c.cookies.set("tangible_dev", "x.y", domain="wb.test")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_oidc_routes_are_404_in_dev_mode(stack: Stack) -> None:  # noqa: F811
    for path in ("/auth/login", "/auth/callback", "/login"):
        r = stack.http.get(path, follow_redirects=False)
        assert r.status_code in (404, 303)
        if path != "/login":
            assert r.status_code == 404
    assert stack.http.post("/auth/logout").status_code == 404


def test_dev_mode_is_unchanged(stack: Stack) -> None:  # noqa: F811
    assert stack.http.app.state.settings.auth_mode == "dev"  # type: ignore[attr-defined]
    assert stack.call("GET", "/api/cases", "ana", 200).json() == []


def test_app_refuses_to_start_without_oidc_settings(
    test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
    ok = {
        "database_url": test_db_url, "evidence_dir": tmp_path, "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID, "oidc_redirect_uri": REDIRECT,
        "oidc_audience": API_AUD, "session_secret": SESSION_SECRET,
    }  # fmt: skip
    assert create_app(Settings(**ok)) is not None
    for missing, env in (
        ("oidc_issuer", "TANGIBLE_OIDC_ISSUER"),
        ("oidc_client_id", "TANGIBLE_OIDC_CLIENT_ID"),
        ("oidc_redirect_uri", "TANGIBLE_OIDC_REDIRECT_URI"),
        ("oidc_audience", "TANGIBLE_OIDC_AUDIENCE"),
        ("session_secret", "TANGIBLE_SESSION_SECRET"),
    ):
        with pytest.raises(RuntimeError, match=env):
            create_app(Settings(**{**ok, missing: None}))
    for bad in (
        {"session_secret": "short"},
        {"oidc_issuer": "http://idp.test"},
        {"oidc_redirect_uri": "http://wb.test/auth/callback"},
        {"oidc_scopes": "email"},
        {"session_hours": 0.0},
        {"oidc_client_secret_ref": "env:DOES_NOT_EXIST_ANYWHERE"},
    ):
        with pytest.raises(RuntimeError, match="refusing to start"):
            create_app(Settings(**{**ok, **bad}))
    assert create_app(
        Settings(**{**ok, "oidc_redirect_uri": "http://localhost:8000/auth/callback"})
    )


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, test_db_url: str) -> None:
    for k, v in {
        "TANGIBLE_OIDC_ISSUER": ISSUER, "TANGIBLE_OIDC_CLIENT_ID": CLIENT_ID,
        "TANGIBLE_OIDC_REDIRECT_URI": REDIRECT, "TANGIBLE_SESSION_SECRET": SESSION_SECRET,
        "TANGIBLE_OIDC_CLIENT_SECRET_REF": "env:OIDC_TEST_SECRET", "TANGIBLE_SESSION_HOURS": "2",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TANGIBLE_DEV_AUTH", raising=False)
    monkeypatch.delenv("TANGIBLE_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
    s = Settings.from_env()
    assert s.auth_mode == "oidc" and s.oidc_scopes == "openid email profile"
    assert s.oidc_tenant_claim == "tangible_tenant" and s.session_hours == 2
    assert s.secure_cookies is True
    monkeypatch.setenv("TANGIBLE_COOKIE_SECURE", "false")
    assert Settings.from_env().secure_cookies is False
    monkeypatch.setenv("TANGIBLE_DEV_AUTH", "1")
    assert Settings.from_env().auth_mode == "dev"


def test_audience_must_be_set_and_differ_from_client_id(
    stack: Stack,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
    with pytest.raises(RuntimeError, match="TANGIBLE_OIDC_AUDIENCE"):
        create_app(make_settings(stack.w, tmp_path, oidc_audience=None))
    with pytest.raises(RuntimeError, match="not the client id"):
        create_app(make_settings(stack.w, tmp_path, oidc_audience=CLIENT_ID))


def test_id_token_is_not_an_api_bearer(oidc: Oidc, idp: FakeIdp) -> None:
    tok = idp.token(oidc.w.tenant, aud=API_AUD, nonce="n-123")
    r = oidc.http.get("/api/cases", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_secrets_never_appear_in_repr(oidc: Oidc) -> None:
    s = oidc.app.state.settings
    shown = repr(s) + repr(oidc.app.state.oidc.config) + repr(oidc.app.state.oidc.provider.config)
    assert CLIENT_SECRET not in shown and SESSION_SECRET not in shown
    assert "env:OIDC_TEST_SECRET" in repr(s)  # the ref is fine; the value is not
    assert oidc.app.state.oidc.config.client_secret == CLIENT_SECRET  # but it was resolved


def test_cookie_secure_follows_setting(
    stack: Stack,  # noqa: F811
    idp: FakeIdp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OIDC_TEST_SECRET", CLIENT_SECRET)
    app = create_app(
        make_settings(stack.w, tmp_path, cookie_secure=False), http_client=idp.client()
    )
    r = TestClient(app).get("/auth/login", follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()


def test_verify_jwt_is_directly_usable(idp: FakeIdp) -> None:
    p = OidcProvider(config(), idp.client())
    t = str(uuid.uuid4())
    assert p.verify_jwt(idp.token(t), audience=API_AUD)["sub"] == "ana"
    with pytest.raises(Unauthenticated):
        p.verify_jwt(idp.token(t), audience=API_AUD, nonce="n")  # id-token mode needs a nonce
    ok = idp.token(t, aud=API_AUD, nonce="n1")
    assert p.verify_jwt(ok, audience=API_AUD, nonce="n1")["nonce"] == "n1"
