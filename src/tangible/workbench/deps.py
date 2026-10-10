"""FastAPI dependencies: the authenticated principal and a tenant-scoped connection."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from tangible.db.session import Conn, tenant_session
from tangible.workbench.auth import Authenticator, Principal, Unauthenticated
from tangible.workbench.settings import Settings


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_principal(request: Request) -> Principal:
    auth: Authenticator = request.app.state.authenticator
    try:
        return auth.authenticate(request.headers)
    except Unauthenticated as e:
        raise HTTPException(401, str(e)) from None
    except NotImplementedError as e:
        raise HTTPException(401, str(e)) from None


PrincipalDep = Annotated[Principal, Depends(get_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_conn(principal: PrincipalDep, settings: SettingsDep) -> Iterator[Conn]:
    """One transaction per request, under RLS for the caller's tenant. Rolls back on any error."""
    with tenant_session(settings.database_url, principal.tenant_id) as conn:
        yield conn


ConnDep = Annotated[Conn, Depends(get_conn)]
