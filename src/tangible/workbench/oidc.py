"""OpenID Connect for the workbench API: discovery, JWKS and bearer-token verification.

The API is the only OIDC surface: callers send JWT access tokens (`Authorization: Bearer`) and
the browser sign-in happens in the web app (Clerk, see `clerk.py`) or the caller's own client.

Assumptions:
  * One IdP per deployment. `sub` is stored as app_user.subject without the issuer.
  * Opaque access tokens are rejected.
  * The tenant is a UUID in a claim (default `tangible_tenant`) of the verified token.
  * Roles and access come only from app_user, looked up on every request. Never from claims.

Errors never include token material. Reasons for rejection are logged at DEBUG only.
"""

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from tangible.workbench.auth import OidcAuthenticator, ProviderError, Unauthenticated
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
_GENERIC = "invalid token"


def _fail(reason: str) -> Unauthenticated:
    log.debug("token rejected: %s", reason)
    return Unauthenticated(_GENERIC)


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    audience: str
    tenant_claim: str
    allow_insecure_http: bool = False


def _is_https(url: str, allow_http: bool) -> bool:
    parts = urlsplit(url)
    if not parts.netloc or parts.fragment:
        return False
    return parts.scheme == "https" or (allow_http and parts.scheme == "http")


_REQUIRED = (
    ("oidc_issuer", "TANGIBLE_OIDC_ISSUER"),
    ("oidc_client_id", "TANGIBLE_OIDC_CLIENT_ID"),
    ("oidc_audience", "TANGIBLE_OIDC_AUDIENCE"),
)


def missing_settings(settings: Settings) -> list[str]:
    return [env for attr, env in _REQUIRED if not getattr(settings, attr)]


def config_from_settings(settings: Settings) -> OidcConfig:
    """Validate the OIDC settings. Raises RuntimeError."""
    missing = missing_settings(settings)
    if missing:
        raise RuntimeError(
            "refusing to start: OIDC auth is not configured (missing " + ", ".join(missing) + "). "
            "Set TANGIBLE_DEV_AUTH=1 on a development machine to use dev header auth instead."
        )
    assert settings.oidc_issuer and settings.oidc_client_id and settings.oidc_audience
    http = settings.oidc_allow_insecure_http
    if not _is_https(settings.oidc_issuer, http):
        raise RuntimeError("refusing to start: TANGIBLE_OIDC_ISSUER must be an https URL")
    if settings.oidc_audience == settings.oidc_client_id:
        # Otherwise any user's ID token (aud = client id) would also pass as an API access token.
        raise RuntimeError(
            "refusing to start: TANGIBLE_OIDC_AUDIENCE must be the API's own audience, "
            "not the client id"
        )
    return OidcConfig(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        audience=settings.oidc_audience,
        tenant_claim=settings.oidc_tenant_claim,
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
            jwks = doc.get("jwks_uri")
            if not isinstance(jwks, str) or not _is_https(jwks, c.allow_insecure_http):
                raise ProviderError("discovery document has no usable jwks_uri")
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


def build_oidc(
    settings: Settings,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> OidcAuthenticator:
    """Validate settings and construct the API authenticator."""
    config = config_from_settings(settings)
    provider = OidcProvider(config, client, clock)
    return OidcAuthenticator(
        settings.database_url, provider, audience=config.audience, tenant_claim=config.tenant_claim
    )
