"""Clerk auth for the API (TANGIBLE_AUTH_MODE=clerk).

The web app (Next.js + Clerk) sends the user's Clerk session token as `Authorization: Bearer`.
It is a short-lived JWT signed by the instance's keys, published at
`<frontend API URL>/.well-known/jwks.json`. We verify signature, issuer, expiry and `azp` (the
origin that minted it must be one of ours). Clerk session tokens carry no `aud`.

Identity only comes from the token. Tenant and role come from app_user: a subject's memberships are
looked up (`memberships_for_subject`), and a client in several tenants picks one with
X-Tangible-Tenant. An account with no membership is an `Identity` only, which is enough to accept
an invite and nothing else. Optional `email` claim: add `"email": "{{user.primary_email_address}}"`
to the instance's session token template.
"""

from collections.abc import Mapping
from typing import Any

import httpx
import jwt

from tangible.workbench.auth import (
    TENANT_HEADER,
    Identity,
    Principal,
    ProviderError,
    Unauthenticated,
    choose_tenant,
)
from tangible.workbench.oidc import (
    ALLOWED_ALGS,
    LEEWAY_SECONDS,
    MAX_TOKEN_CHARS,
    OidcConfig,
    OidcProvider,
    _fail,
    _is_https,
)
from tangible.workbench.settings import Settings


class ClerkVerifier(OidcProvider):
    """The OIDC provider's JWKS cache and key handling, with Clerk's session-token rules."""

    def __init__(
        self,
        issuer: str,
        authorized_parties: tuple[str, ...],
        client: httpx.Client | None = None,
        *,
        allow_insecure_http: bool = False,
    ) -> None:
        super().__init__(
            OidcConfig(
                issuer=issuer.rstrip("/"),
                client_id="",
                redirect_uri="",
                audience="",
                scopes="",
                tenant_claim="",
                allow_insecure_http=allow_insecure_http,
            ),  # fmt: skip
            client,
        )
        self.authorized_parties = frozenset(a.rstrip("/") for a in authorized_parties)

    def metadata(self) -> dict[str, Any]:
        # Clerk publishes JWKS at a fixed path; no discovery round trip is needed.
        if not _is_https(self.config.issuer, self.config.allow_insecure_http):
            raise ProviderError("Clerk issuer must be an https URL")
        return {"jwks_uri": self.config.issuer + "/.well-known/jwks.json"}

    def verify_session(self, token: str) -> dict[str, Any]:
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
        key = self._key_from_jwk(self._jwk_for(kid), alg)
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=[alg], issuer=self.config.issuer, leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False},
            )  # fmt: skip
        except jwt.PyJWTError as e:
            raise _fail(type(e).__name__) from None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise _fail("bad sub")
        azp = claims.get("azp")
        if self.authorized_parties and (
            not isinstance(azp, str) or azp.rstrip("/") not in self.authorized_parties
        ):
            raise _fail("azp not authorized")
        return claims


class ClerkAuthenticator:
    def __init__(self, conninfo: str, verifier: ClerkVerifier) -> None:
        self.conninfo = conninfo
        self.verifier = verifier

    def _claims(self, headers: Mapping[str, str]) -> dict[str, Any]:
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("a Bearer token is required")
        return self.verifier.verify_session(token)

    def identify(self, headers: Mapping[str, str]) -> Identity:
        claims = self._claims(headers)
        email = claims.get("email")
        return Identity(claims["sub"], email if isinstance(email, str) and email else None)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        sub = self._claims(headers)["sub"]
        return choose_tenant(self.conninfo, sub, headers.get(TENANT_HEADER, "").strip() or None)


def build_clerk(settings: Settings, client: httpx.Client | None = None) -> ClerkAuthenticator:
    if not settings.clerk_issuer:
        raise RuntimeError(
            "refusing to start: TANGIBLE_AUTH_MODE=clerk needs TANGIBLE_CLERK_ISSUER"
        )
    if not settings.clerk_authorized_parties:
        raise RuntimeError(
            "refusing to start: TANGIBLE_AUTH_MODE=clerk needs TANGIBLE_CLERK_AUTHORIZED_PARTIES "
            "(the web app's origins, comma-separated)"
        )
    verifier = ClerkVerifier(settings.clerk_issuer, settings.clerk_authorized_parties, client)
    return ClerkAuthenticator(settings.database_url, verifier)
