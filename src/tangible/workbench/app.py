"""The workbench FastAPI app: `create_app(settings)` and the `tangible-workbench` entry point."""

import os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tangible.workbench import services
from tangible.workbench.api import assessments, backtest, cases, dashboard, invites, me, onboarding
from tangible.workbench.auth import Authenticator, DevHeaderAuthenticator, OidcAuthenticator
from tangible.workbench.clerk import build_clerk
from tangible.workbench.gate import PasswordGate
from tangible.workbench.oidc import build_oidc
from tangible.workbench.settings import Settings

_STATUS: dict[type[Exception], int] = {
    services.NotFound: 404,
    services.Forbidden: 403,
    services.Conflict: 409,
    services.Invalid: 422,
}


def create_app(
    settings: Settings | None = None,
    authenticator: Authenticator | None = None,
    *,
    http_client: httpx.Client | None = None,
) -> FastAPI:
    """Build the app: the JSON API under /api plus /healthz. Auth is dev headers, OIDC bearer
    tokens (the default; its settings are required and validated) or Clerk session tokens.
    `http_client` is the injectable client for IdP requests (tests). An explicitly passed
    `authenticator` replaces the configured one (tests only)."""
    settings = settings or Settings.from_env()
    if settings.access_password and settings.auth_mode != "dev":
        raise ValueError("TANGIBLE_ACCESS_PASSWORD is for dev-auth demos only")
    if authenticator is None:
        if settings.auth_mode == "dev":
            authenticator = DevHeaderAuthenticator(settings.database_url)
        elif settings.auth_mode == "clerk":
            authenticator = build_clerk(settings, http_client)
        else:
            authenticator = build_oidc(settings, http_client)
    app = FastAPI(title="Tangible workbench API")
    app.state.settings = settings
    app.state.authenticator = authenticator

    async def domain_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=_STATUS[type(exc)])

    for exc_type in _STATUS:
        app.add_exception_handler(exc_type, domain_error)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(cases.router)
    app.include_router(assessments.router)
    app.include_router(me.router)
    app.include_router(dashboard.router)
    app.include_router(invites.router)
    app.include_router(onboarding.router)
    app.include_router(backtest.router)
    if settings.access_password:
        app.add_middleware(PasswordGate, password=settings.access_password)
    return app


def main() -> None:
    app = create_app(Settings.from_env())
    uvicorn.run(
        app, host=os.environ.get("TANGIBLE_HOST", "127.0.0.1"),
        # PORT is what hosting platforms (Railway, Heroku) assign; TANGIBLE_PORT wins if set.
        port=int(os.environ.get("TANGIBLE_PORT") or os.environ.get("PORT") or "8000"),
    )  # fmt: skip


__all__ = ["OidcAuthenticator", "create_app", "main"]
