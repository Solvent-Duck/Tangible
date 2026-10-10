"""The workbench FastAPI app: `create_app(settings)` and the `tangible-workbench` entry point."""

import os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tangible.workbench import services, ui
from tangible.workbench.api import assessments, cases
from tangible.workbench.auth import Authenticator, DevHeaderAuthenticator, OidcAuthenticator
from tangible.workbench.oidc import OidcRuntime, build_runtime, missing_settings
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
    """Build the app. In oidc mode (the default) the OIDC settings are required and validated;
    `http_client` is the injectable client for IdP requests (tests). An explicitly passed
    `authenticator` replaces the API authenticator (tests only)."""
    settings = settings or Settings.from_env()
    runtime: OidcRuntime | None = None
    if settings.auth_mode == "dev":
        authenticator = authenticator or DevHeaderAuthenticator(settings.database_url)
    elif authenticator is None or not missing_settings(settings):
        runtime, built = build_runtime(settings, http_client)
        authenticator = authenticator or built
    app = FastAPI(title="Tangible workbench")
    app.state.settings = settings
    app.state.authenticator = authenticator
    app.state.oidc = runtime  # None in dev mode

    async def domain_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=_STATUS[type(exc)])

    for exc_type in _STATUS:
        app.add_exception_handler(exc_type, domain_error)
    app.include_router(cases.router)
    app.include_router(assessments.router)
    ui.install(app)
    return app


def main() -> None:
    app = create_app(Settings.from_env())
    uvicorn.run(
        app, host=os.environ.get("TANGIBLE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TANGIBLE_PORT", "8000")),
    )  # fmt: skip


__all__ = ["OidcAuthenticator", "create_app", "main"]
