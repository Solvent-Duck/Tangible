"""Server-rendered analyst workbench UI (Jinja2 + htmx). All logic lives in `workbench.services`."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from tangible.workbench.ui.routes import STATIC_DIR, router, setup


def install(app: FastAPI) -> None:
    """Mount the UI routes and static files on the workbench app."""
    setup(app)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


__all__ = ["install", "router"]
