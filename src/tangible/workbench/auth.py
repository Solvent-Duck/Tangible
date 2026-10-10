"""Authentication: who is calling, as which role, in which tenant.

Two modes. Dev (TANGIBLE_DEV_AUTH=1) trusts two headers and is NOT safe outside development.
OIDC (the default) verifies bearer JWTs for the API (`OidcAuthenticator`) and uses an
authorization-code login for the UI (see `oidc.py`). Roles always come from app_user.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tangible.db.session import tenant_session

Role = Literal["analyst", "lender_underwriter", "credit_admin"]
TENANT_HEADER = "X-Tangible-Tenant"
USER_HEADER = "X-Tangible-User"


class Unauthenticated(Exception):
    """No valid credentials (HTTP 401)."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    user_id: str
    role: Role


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


class DevHeaderAuthenticator:
    """Dev only: tenant and subject come from headers and are looked up in app_user."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        tenant = headers.get(TENANT_HEADER, "").strip()
        subject = headers.get(USER_HEADER, "").strip()
        if not tenant or not subject:
            raise Unauthenticated(f"{TENANT_HEADER} and {USER_HEADER} are required")
        try:
            tenant = str(uuid.UUID(tenant))
        except ValueError:
            raise Unauthenticated("unknown user") from None
        with tenant_session(self.conninfo, tenant) as conn:
            row = conn.execute(
                "SELECT id, role FROM app_user WHERE subject = %s", (subject,)
            ).fetchone()
        if row is None:
            raise Unauthenticated("unknown user")
        return Principal(subject, tenant, str(row[0]), row[1])


def lookup_principal(conninfo: str, tenant: str, subject: str) -> Principal:
    """Resolve (tenant, subject) to a Principal from app_user. The role always comes from the
    database. No auto-provisioning: an unknown subject is unauthenticated."""
    with tenant_session(conninfo, tenant) as conn:
        row = conn.execute(
            "SELECT id, role FROM app_user WHERE subject = %s", (subject,)
        ).fetchone()
    if row is None:
        raise Unauthenticated("unknown user")
    return Principal(subject, tenant, str(row[0]), row[1])


def principal_from_claims(conninfo: str, tenant_claim: str, claims: Mapping[str, Any]) -> Principal:
    """Map verified token claims to a Principal. The tenant comes from `tenant_claim` (a UUID);
    `sub` is stored as app_user.subject (one IdP per deployment is assumed, so it is not
    issuer-qualified). Nothing but identity is read from the token: roles are never taken from
    claims."""
    raw_tenant = claims.get(tenant_claim)
    sub = claims.get("sub")
    if not isinstance(raw_tenant, str) or not isinstance(sub, str) or not sub:
        raise Unauthenticated("invalid token")
    try:
        tenant = str(uuid.UUID(raw_tenant))
    except ValueError:
        raise Unauthenticated("invalid token") from None
    return lookup_principal(conninfo, tenant, sub)


class ProviderError(Exception):
    """The identity provider could not be reached or returned something unusable (HTTP 503)."""


class TokenVerifier(Protocol):
    def verify_jwt(
        self, token: str, *, audience: str, nonce: str | None = None
    ) -> Mapping[str, Any]: ...


class OidcAuthenticator:
    """API authentication: `Authorization: Bearer <JWT access token>`.

    The token is verified against the IdP's JWKS (signature, issuer, audience, expiry), then
    mapped to an app_user. Opaque (non-JWT) access tokens are not supported: configure the IdP to
    issue JWT access tokens for this API.
    """

    def __init__(
        self, conninfo: str, verifier: TokenVerifier, *, audience: str, tenant_claim: str
    ) -> None:
        self.conninfo = conninfo
        self.verifier = verifier
        self.audience = audience
        self.tenant_claim = tenant_claim

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("a Bearer token is required")
        if token.count(".") != 2:
            raise Unauthenticated(
                "JWT access tokens are required (opaque tokens are not supported)"
            )
        claims = self.verifier.verify_jwt(token, audience=self.audience)
        return principal_from_claims(self.conninfo, self.tenant_claim, claims)
