"""Authentication: who is calling, as which role, in which tenant.

v1 is a dev stub that trusts two headers. It is NOT safe outside development, so the app refuses
to start with it unless TANGIBLE_DEV_AUTH=1. The production path is the `OidcAuthenticator`
interface below.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

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


class OidcAuthenticator:
    """Production authentication.

    TODO(OIDC): verify the bearer JWT against the lender IdP's JWKS (issuer, audience, expiry),
    map the verified `sub` and tenant claim to an app_user row, and return a Principal. Until this
    is implemented the workbench must only run with dev auth on a developer machine.
    """

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        raise NotImplementedError("OIDC verification is not implemented yet")
