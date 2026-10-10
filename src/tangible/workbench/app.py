"""The workbench FastAPI app: `create_app(settings)` and the `tangible-workbench` entry point."""

import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tangible.workbench import services
from tangible.workbench.api import assessments, cases
from tangible.workbench.auth import Authenticator, DevHeaderAuthenticator, OidcAuthenticator
from tangible.workbench.settings import Settings

_STATUS: dict[type[Exception], int] = {
    services.NotFound: 404,
    services.Forbidden: 403,
    services.Conflict: 409,
    services.Invalid: 422,
}


def create_app(
    settings: Settings | None = None, authenticator: Authenticator | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    if authenticator is None:
        if not settings.dev_auth:
            # Dev header auth is not real authentication; OIDC is not built yet (see auth.py).
            raise RuntimeError(
                "refusing to start: the workbench only has dev header auth so far. "
                "Set TANGIBLE_DEV_AUTH=1 on a development machine to use it."
            )
        authenticator = DevHeaderAuthenticator(settings.database_url)
    app = FastAPI(title="Tangible workbench")
    app.state.settings = settings
    app.state.authenticator = authenticator

    async def domain_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=_STATUS[type(exc)])

    for exc_type in _STATUS:
        app.add_exception_handler(exc_type, domain_error)
    app.include_router(cases.router)
    app.include_router(assessments.router)
    return app


def main() -> None:
    app = create_app(Settings.from_env())
    uvicorn.run(
        app, host=os.environ.get("TANGIBLE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TANGIBLE_PORT", "8000")),
    )  # fmt: skip


__all__ = ["OidcAuthenticator", "create_app", "main"]
