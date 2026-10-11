"""Authentication: who is calling, as which role, in which tenant.

Three modes. Dev (TANGIBLE_DEV_AUTH=1) trusts two headers and is NOT safe outside development.
OIDC (the default) verifies bearer JWTs for the API (`OidcAuthenticator`) and uses an
authorization-code login for the UI (see `oidc.py`). Clerk (TANGIBLE_AUTH_MODE=clerk) verifies
Clerk session JWTs and finds the tenant from the user's memberships (see `clerk.py`). Roles always
come from app_user.

An `Identity` is a verified subject with no tenant yet: what a borrower has between creating an
account and accepting an invite. Only identity-only routes (`/api/me`, invite acceptance) take one.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tangible.db.session import tenant_session

Role = Literal["analyst", "lender_underwriter", "credit_admin", "borrower"]
LENDER_ROLES: frozenset[str] = frozenset({"credit_admin", "lender_underwriter"})
STAFF_ROLES: frozenset[str] = frozenset({"analyst", "credit_admin", "lender_underwriter"})
TENANT_HEADER = "X-Tangible-Tenant"
USER_HEADER = "X-Tangible-User"
EMAIL_HEADER = "X-Tangible-Email"  # dev mode only: the email a dev identity claims


class Unauthenticated(Exception):
    """No valid credentials (HTTP 401)."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    user_id: str
    role: Role
    borrower_id: str | None = None  # set for (and only for) role == "borrower"


@dataclass(frozen=True)
class Identity:
    """A verified subject, before any tenant is chosen."""

    subject: str
    email: str | None = None


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...

    def identify(self, headers: Mapping[str, str]) -> Identity: ...


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
        return lookup_principal(self.conninfo, tenant, subject)

    def identify(self, headers: Mapping[str, str]) -> Identity:
        subject = headers.get(USER_HEADER, "").strip()
        if not subject:
            raise Unauthenticated(f"{USER_HEADER} is required")
        return Identity(subject, headers.get(EMAIL_HEADER, "").strip() or None)


def lookup_principal(conninfo: str, tenant: str, subject: str) -> Principal:
    """Resolve (tenant, subject) to a Principal from app_user. The role always comes from the
    database. No auto-provisioning: an unknown subject is unauthenticated (a borrower becomes a
    user only by accepting an invite)."""
    with tenant_session(conninfo, tenant) as conn:
        row = conn.execute(
            "SELECT id, role, borrower_id FROM app_user WHERE subject = %s", (subject,)
        ).fetchone()
    if row is None:
        raise Unauthenticated("unknown user")
    return Principal(subject, tenant, str(row[0]), row[1], str(row[2]) if row[2] else None)


@dataclass(frozen=True)
class Membership:
    tenant_id: str
    tenant_name: str
    role: Role


def memberships(conninfo: str, subject: str) -> list[Membership]:
    """Every tenant this subject is a user in, newest tenant first. Asked before a tenant is
    known, so it goes through the `memberships_for_subject` SECURITY DEFINER function (0010)."""
    with tenant_session(conninfo, "") as conn:
        rows = conn.execute("SELECT * FROM memberships_for_subject(%s)", (subject,)).fetchall()
    return [Membership(str(t), str(n), r) for t, n, r in rows]


def choose_tenant(conninfo: str, subject: str, requested: str | None) -> Principal:
    """The Principal for `subject` in the requested tenant (which must be one of its memberships)
    or, with none requested, its only membership. Ambiguity is an error the client resolves by
    sending X-Tangible-Tenant."""
    mine = memberships(conninfo, subject)
    if requested:
        try:
            requested = str(uuid.UUID(requested))
        except ValueError:
            raise Unauthenticated("unknown tenant") from None
        if requested not in {m.tenant_id for m in mine}:
            raise Unauthenticated("not a member of that tenant")
        return lookup_principal(conninfo, requested, subject)
    if not mine:
        raise Unauthenticated("unknown user")
    if len(mine) > 1:
        raise Unauthenticated(f"member of several tenants: send {TENANT_HEADER}")
    return lookup_principal(conninfo, mine[0].tenant_id, subject)


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
        return principal_from_claims(self.conninfo, self.tenant_claim, self._claims(headers))

    def identify(self, headers: Mapping[str, str]) -> Identity:
        claims = self._claims(headers)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise Unauthenticated("invalid token")
        email = claims.get("email")
        return Identity(sub, email if isinstance(email, str) and email else None)

    def _claims(self, headers: Mapping[str, str]) -> Mapping[str, Any]:
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("a Bearer token is required")
        if token.count(".") != 2:
            raise Unauthenticated(
                "JWT access tokens are required (opaque tokens are not supported)"
            )
        claims = self.verifier.verify_jwt(token, audience=self.audience)
        if "nonce" in claims:
            # ID tokens carry the login nonce; only access tokens may call the API.
            raise Unauthenticated("an access token is required, not an ID token")
        return claims
