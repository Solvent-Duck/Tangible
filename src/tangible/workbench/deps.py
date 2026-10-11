"""FastAPI dependencies: the authenticated principal and a tenant-scoped connection."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from tangible.db.session import Conn, tenant_session
from tangible.workbench.auth import (
    Authenticator,
    Identity,
    Principal,
    ProviderError,
    Unauthenticated,
)
from tangible.workbench.settings import Settings


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_principal(request: Request) -> Principal:
    auth: Authenticator = request.app.state.authenticator
    try:
        return auth.authenticate(request.headers)
    except Unauthenticated as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from None
    except ProviderError:
        raise HTTPException(503, "identity provider unavailable") from None


PrincipalDep = Annotated[Principal, Depends(get_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_conn(principal: PrincipalDep, settings: SettingsDep) -> Iterator[Conn]:
    """One transaction per request, under RLS for the caller's tenant. Rolls back on any error."""
    with tenant_session(settings.database_url, principal.tenant_id) as conn:
        yield conn


ConnDep = Annotated[Conn, Depends(get_conn)]


def get_identity(request: Request) -> Identity:
    """A verified subject with no tenant: for routes a new borrower uses before it is a user."""
    auth: Authenticator = request.app.state.authenticator
    try:
        return auth.identify(request.headers)
    except Unauthenticated as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from None
    except ProviderError:
        raise HTTPException(503, "identity provider unavailable") from None


IdentityDep = Annotated[Identity, Depends(get_identity)]
