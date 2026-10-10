"""OpenID Connect for the workbench: provider metadata, JWT verification, login and sessions.

Assumptions (see docs in the PR notes):
  * One IdP per deployment. `sub` is stored as app_user.subject without the issuer.
  * API callers send JWT access tokens (`Authorization: Bearer`); opaque tokens are rejected.
  * The tenant is a UUID in a claim (default `tangible_tenant`) of the verified token. The ID token
    used for the browser login must carry that claim too.
  * Roles and access come only from app_user, looked up on every request. Never from claims.

Errors never include token material. Reasons for rejection are logged at DEBUG only.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from tangible.secrets import SecretNotFound
from tangible.workbench.auth import (
    OidcAuthenticator,
    Principal,
    ProviderError,
    Unauthenticated,
    lookup_principal,
    principal_from_claims,
)
from tangible.workbench.settings import Settings

log = logging.getLogger(__name__)

ALLOWED_ALGS = ("RS256", "PS256", "ES256")
_KTY_FOR_ALG = {"RS256": "RSA", "PS256": "RSA", "ES256": "EC"}
LEEWAY_SECONDS = 60
JWKS_MIN_REFETCH_SECONDS = 60.0  # at most one refetch per this window (unknown-kid rate limit)
JWKS_MAX_AGE_SECONDS = 3600.0  # known keys are re-read at least this often
METADATA_MAX_AGE_SECONDS = 3600.0
HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
MAX_RESPONSE_BYTES = 1_000_000
MAX_TOKEN_CHARS = 16_384
LOGIN_TTL_SECONDS = 600

SESSION_COOKIE = "tangible_session"
LOGIN_COOKIE = "tangible_login"
_GENERIC = "invalid token"


class LoginStateError(Exception):
    """The callback does not belong to a login this browser started (HTTP 400)."""


def _fail(reason: str) -> Unauthenticated:
    log.debug("token rejected: %s", reason)
    return Unauthenticated(_GENERIC)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    redirect_uri: str
    audience: str
    scopes: str
    tenant_claim: str
    client_secret: str | None = field(default=None, repr=False)
    allow_insecure_http: bool = False


def _is_https(url: str, allow_http: bool) -> bool:
    parts = urlsplit(url)
    if not parts.netloc or parts.fragment:
        return False
    return parts.scheme == "https" or (allow_http and parts.scheme == "http")


def _is_loopback_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


_REQUIRED = (
    ("oidc_issuer", "TANGIBLE_OIDC_ISSUER"),
    ("oidc_client_id", "TANGIBLE_OIDC_CLIENT_ID"),
    ("oidc_redirect_uri", "TANGIBLE_OIDC_REDIRECT_URI"),
    ("oidc_audience", "TANGIBLE_OIDC_AUDIENCE"),
    ("session_secret", "TANGIBLE_SESSION_SECRET"),
)


def missing_settings(settings: Settings) -> list[str]:
    return [env for attr, env in _REQUIRED if not getattr(settings, attr)]


def config_from_settings(settings: Settings) -> OidcConfig:
    """Validate the OIDC settings and resolve the client secret. Raises RuntimeError."""
    missing = missing_settings(settings)
    if missing:
        raise RuntimeError(
            "refusing to start: OIDC auth is not configured (missing " + ", ".join(missing) + "). "
            "Set TANGIBLE_DEV_AUTH=1 on a development machine to use dev header auth instead."
        )
    assert settings.oidc_issuer and settings.oidc_client_id and settings.oidc_redirect_uri
    assert settings.session_secret
    http = settings.oidc_allow_insecure_http
    if not _is_https(settings.oidc_issuer, http):
        raise RuntimeError("refusing to start: TANGIBLE_OIDC_ISSUER must be an https URL")
    if not (
        _is_https(settings.oidc_redirect_uri, http) or _is_loopback_http(settings.oidc_redirect_uri)
    ):
        raise RuntimeError(
            "refusing to start: TANGIBLE_OIDC_REDIRECT_URI must be https (http only on localhost)"
        )
    if len(settings.session_secret.encode()) < 32:
        raise RuntimeError("refusing to start: TANGIBLE_SESSION_SECRET must be at least 32 bytes")
    if not settings.session_hours > 0:
        raise RuntimeError("refusing to start: TANGIBLE_SESSION_HOURS must be positive")
    assert settings.oidc_audience
    if settings.oidc_audience == settings.oidc_client_id:
        # Otherwise any user's ID token (aud = client id) would also pass as an API access token.
        raise RuntimeError(
            "refusing to start: TANGIBLE_OIDC_AUDIENCE must be the API's own audience, "
            "not the client id"
        )
    if "openid" not in settings.oidc_scopes.split():
        raise RuntimeError("refusing to start: TANGIBLE_OIDC_SCOPES must include 'openid'")
    secret = None
    if settings.oidc_client_secret_ref:
        try:
            secret = settings.secret_store.get(settings.oidc_client_secret_ref)
        except (SecretNotFound, ValueError):
            raise RuntimeError(
                "refusing to start: cannot resolve TANGIBLE_OIDC_CLIENT_SECRET_REF "
                f"({settings.oidc_client_secret_ref!r})"
            ) from None
    return OidcConfig(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        redirect_uri=settings.oidc_redirect_uri,
        audience=settings.oidc_audience,
        scopes=settings.oidc_scopes,
        tenant_claim=settings.oidc_tenant_claim,
        client_secret=secret,
        allow_insecure_http=http,
    )


# ---------------------------------------------------------------- provider


class OidcProvider:
    """Discovery, JWKS cache and token verification for one issuer."""

    def __init__(
        self,
        config: OidcConfig,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False)
        self.clock = clock
        self._lock = threading.RLock()
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_at: float | None = None  # when JWKS was last *attempted*; None: never

    # -- HTTP

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            r = self.client.get(
                url, timeout=HTTP_TIMEOUT, follow_redirects=False,
                headers={"Accept": "application/json"},
            )  # fmt: skip
        except httpx.HTTPError as e:
            log.debug("GET %s failed: %s", url, type(e).__name__)
            raise ProviderError("identity provider unavailable") from None
        if r.status_code != 200 or len(r.content) > MAX_RESPONSE_BYTES:
            log.debug("GET %s -> %s", url, r.status_code)
            raise ProviderError("identity provider returned an unusable response")
        try:
            data = r.json()
        except ValueError:
            raise ProviderError("identity provider returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("identity provider returned invalid JSON")
        return data

    # -- discovery

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            if self._metadata is not None and now - self._metadata_at < METADATA_MAX_AGE_SECONDS:
                return self._metadata
            c = self.config
            if not _is_https(c.issuer, c.allow_insecure_http):
                raise ProviderError("OIDC issuer must be an https URL")
            doc = self._get_json(c.issuer.rstrip("/") + "/.well-known/openid-configuration")
            if doc.get("issuer") != c.issuer:  # exact match (RFC 8414 / OIDC Discovery 4.3)
                raise ProviderError("discovery document issuer does not match configuration")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                v = doc.get(key)
                if not isinstance(v, str) or not _is_https(v, c.allow_insecure_http):
                    raise ProviderError(f"discovery document has no usable {key}")
            end = doc.get("end_session_endpoint")
            if end is not None and not (
                isinstance(end, str) and _is_https(end, c.allow_insecure_http)
            ):
                doc.pop("end_session_endpoint")
            self._metadata, self._metadata_at = doc, now
            return doc

    # -- JWKS

    def _fetch_jwks(self) -> None:
        # Caller holds the lock. Recorded before the request so failures are rate-limited too.
        self._keys_at = self.clock()
        doc = self._get_json(self.metadata()["jwks_uri"])
        keys = doc.get("keys")
        if not isinstance(keys, list):
            raise ProviderError("JWKS has no keys")
        found: dict[str, dict[str, Any]] = {}
        dup: set[str] = set()
        for jwk in keys:
            kid = jwk.get("kid") if isinstance(jwk, dict) else None
            if not isinstance(kid, str) or not kid:
                continue
            if kid in found:
                dup.add(kid)
            found[kid] = jwk
        self._keys = {k: v for k, v in found.items() if k not in dup}

    def _jwk_for(self, kid: str) -> dict[str, Any]:
        with self._lock:
            age = None if self._keys_at is None else self.clock() - self._keys_at
            stale = age is None or age >= JWKS_MAX_AGE_SECONDS
            if kid not in self._keys or stale:
                if age is None or age >= JWKS_MIN_REFETCH_SECONDS:
                    self._fetch_jwks()
            jwk = self._keys.get(kid)
        if jwk is None:
            raise _fail("unknown kid")
        return jwk

    # -- verification

    def verify_jwt(self, token: str, *, audience: str, nonce: str | None = None) -> dict[str, Any]:
        """Verify signature and claims. `nonce` (ID tokens) is required and compared if given."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
            raise _fail("not a compact JWT")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise _fail("malformed header") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALLOWED_ALGS:
            raise _fail(f"algorithm {alg!r} not allowed")
        if not isinstance(kid, str) or not kid:
            raise _fail("missing kid")
        jwk = self._jwk_for(kid)
        key = self._key_from_jwk(jwk, alg)
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=[alg], audience=audience, issuer=self.config.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )  # fmt: skip
        except jwt.PyJWTError as e:
            raise _fail(type(e).__name__) from None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise _fail("bad sub")
        aud = claims["aud"]
        azp = claims.get("azp")
        if isinstance(aud, list) and len(aud) > 1 and azp != self.config.client_id:
            raise _fail("azp mismatch")
        if nonce is not None:
            if azp is not None and azp != self.config.client_id:
                raise _fail("azp mismatch")
            got = claims.get("nonce")
            if not isinstance(got, str) or not hmac.compare_digest(got.encode(), nonce.encode()):
                raise _fail("nonce mismatch")
        return claims

    @staticmethod
    def _key_from_jwk(jwk: dict[str, Any], alg: str) -> Any:
        """Build the verification key; the header alg must match the key type (no alg confusion)."""
        if jwk.get("kty") != _KTY_FOR_ALG[alg]:
            raise _fail("key type does not match alg")
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", alg) != alg:
            raise _fail("key not usable for this alg")
        if alg == "ES256" and jwk.get("crv") != "P-256":
            raise _fail("wrong curve")
        try:
            if alg == "ES256":
                return ECAlgorithm.from_jwk(json.dumps(jwk))
            key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise _fail("unusable JWK") from None
        if key.key_size < 2048:
            raise _fail("RSA key too small")
        return key

    # -- authorization code exchange

    def exchange_code(self, code: str, code_verifier: str) -> str:
        """Redeem the code (client_secret_basic + PKCE) and return the raw ID token."""
        c = self.config
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": c.redirect_uri,
            "code_verifier": code_verifier,
        }
        headers = {"Accept": "application/json"}
        if c.client_secret:
            cred = f"{quote(c.client_id, safe='')}:{quote(c.client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        else:
            data["client_id"] = c.client_id  # public client
        try:
            r = self.client.post(
                self.metadata()["token_endpoint"], data=data, headers=headers,
                timeout=HTTP_TIMEOUT, follow_redirects=False,
            )  # fmt: skip
        except httpx.HTTPError as e:
            log.debug("token endpoint failed: %s", type(e).__name__)
            raise ProviderError("identity provider unavailable") from None
        if 400 <= r.status_code < 500:
            log.debug("token endpoint rejected the code: %s", r.status_code)
            raise Unauthenticated("sign-in failed")
        if r.status_code != 200 or len(r.content) > MAX_RESPONSE_BYTES:
            raise ProviderError("identity provider returned an unusable response")
        try:
            body = r.json()
        except ValueError:
            raise ProviderError("identity provider returned invalid JSON") from None
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str):
            raise _fail("no id_token in token response")
        return id_token


# ---------------------------------------------------------------- signed cookies


class SignedCookies:
    """Stdlib HMAC-SHA256 signed JSON values with an absolute expiry. Not encrypted: never put
    anything in one that the browser's owner must not see."""

    MAX_LEN = 4096

    def __init__(self, secret: bytes, now: Callable[[], float] = time.time) -> None:
        self._secret = secret
        self.now = now

    def _mac(self, purpose: str, body: str) -> str:
        key = hmac.new(self._secret, b"tangible-cookie-v1|" + purpose.encode(), "sha256").digest()
        return _b64(hmac.new(key, body.encode(), "sha256").digest())

    def dumps(self, purpose: str, payload: Mapping[str, Any], ttl: float) -> str:
        now = int(self.now())
        full = {**payload, "p": purpose, "iat": now, "exp": now + int(ttl)}
        body = _b64(json.dumps(full, separators=(",", ":")).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def loads(self, purpose: str, value: str | None) -> dict[str, Any] | None:
        if not value or len(value) > self.MAX_LEN or value.count(".") != 1:
            return None
        body, sig = value.split(".")
        if not hmac.compare_digest(sig.encode(), self._mac(purpose, body).encode()):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("p") != purpose:
            return None
        exp = data.get("exp")
        if not isinstance(exp, int) or self.now() >= exp:
            return None
        return data


# ---------------------------------------------------------------- redirects


_CONTROL = re.compile(r"[\x00-\x1f\x7f\\]")


def safe_next(value: str | None) -> str | None:
    """Only same-site relative paths (no scheme, host, protocol-relative or backslash tricks)."""
    if not value or len(value) > 2000 or _CONTROL.search(value):
        return None
    if not value.startswith("/") or value.startswith("//") or value.startswith("/auth/"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


# ---------------------------------------------------------------- runtime used by the routes


class OidcRuntime:
    """Everything the UI needs: login start/finish, sessions, logout."""

    def __init__(
        self,
        provider: OidcProvider,
        conninfo: str,
        session_secret: str,
        *,
        session_hours: float,
        secure_cookies: bool,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.provider = provider
        self.config = provider.config
        self.conninfo = conninfo
        self.cookies = SignedCookies(session_secret.encode(), now)
        self.session_seconds = int(session_hours * 3600)
        self.secure_cookies = secure_cookies

    # -- login

    def begin_login(self, next_url: str | None) -> tuple[str, str]:
        """Returns (authorization URL, login cookie value)."""
        c = self.config
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43..128
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        endpoint = self.provider.metadata()["authorization_endpoint"]
        query = urlencode(
            {
                "response_type": "code", "client_id": c.client_id, "redirect_uri": c.redirect_uri,
                "scope": c.scopes, "state": state, "nonce": nonce,
                "code_challenge": challenge, "code_challenge_method": "S256",
            }
        )  # fmt: skip
        url = endpoint + ("&" if "?" in endpoint else "?") + query
        cookie = self.cookies.dumps(
            "login",
            {"state": state, "nonce": nonce, "v": verifier, "next": safe_next(next_url)},
            LOGIN_TTL_SECONDS,
        )
        return url, cookie

    def finish_login(
        self, login_cookie: str | None, state: str | None, code: str | None, iss: str | None
    ) -> tuple[Principal, str | None]:
        """Validate the callback and return the principal and the post-login path."""
        data = self.cookies.loads("login", login_cookie)
        if (
            data is None
            or not state
            or not hmac.compare_digest(state.encode(), str(data.get("state", "")).encode())
        ):
            raise LoginStateError("login state mismatch")
        if iss is not None and iss != self.config.issuer:  # RFC 9207 mix-up defence
            raise LoginStateError("issuer mismatch")
        if not code:
            raise LoginStateError("missing code")
        id_token = self.provider.exchange_code(code, str(data["v"]))
        claims = self.provider.verify_jwt(
            id_token, audience=self.config.client_id, nonce=str(data["nonce"])
        )
        principal = principal_from_claims(self.conninfo, self.config.tenant_claim, claims)
        nxt = data.get("next")
        return principal, safe_next(nxt if isinstance(nxt, str) else None)

    # -- sessions

    def issue_session(self, p: Principal) -> str:
        return self.cookies.dumps(
            "session",
            {"t": p.tenant_id, "s": p.subject, "sid": secrets.token_urlsafe(16)},
            self.session_seconds,
        )

    def principal_for_session(self, cookie: str | None) -> Principal:
        """Verify the cookie and re-read app_user, so removals and role changes apply at once."""
        data = self.cookies.loads("session", cookie)
        if data is None:
            raise Unauthenticated("not signed in")
        try:
            tenant = str(uuid.UUID(str(data["t"])))
            subject = data["s"]
        except (KeyError, ValueError):
            raise Unauthenticated("not signed in") from None
        if not isinstance(subject, str):
            raise Unauthenticated("not signed in")
        return lookup_principal(self.conninfo, tenant, subject)

    # -- logout

    def logout_url(self) -> str | None:
        """The provider's RP-initiated logout URL, if it advertises one."""
        try:
            end = self.provider.metadata().get("end_session_endpoint")
        except ProviderError:
            return None
        if not isinstance(end, str):
            return None
        root = urlsplit(self.config.redirect_uri)
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "post_logout_redirect_uri": f"{root.scheme}://{root.netloc}/",
            }
        )
        return end + ("&" if "?" in end else "?") + query


def build_runtime(
    settings: Settings,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
) -> tuple[OidcRuntime, OidcAuthenticator]:
    """Validate settings and construct the OIDC runtime plus the API authenticator."""
    config = config_from_settings(settings)
    provider = OidcProvider(config, client, clock)
    assert settings.session_secret
    runtime = OidcRuntime(
        provider, settings.database_url, settings.session_secret,
        session_hours=settings.session_hours, secure_cookies=settings.secure_cookies, now=now,
    )  # fmt: skip
    authenticator = OidcAuthenticator(
        settings.database_url, provider, audience=config.audience, tenant_claim=config.tenant_claim
    )
    return runtime, authenticator
