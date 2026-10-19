"""FastAPI application factory (owner: lead).

One deployment serves the API under /api and, when built, the web app from apps/web/dist.

Feature packages may expose ``async def startup(app)`` / ``async def shutdown(app)`` in the
modules listed in FEATURE_MODULES to attach their singletons to ``app.state``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import (
    routes_agent,
    routes_cases,
    routes_experiments,
    routes_learn,
    routes_meta,
    routes_revisions,
)
from .api.deps import install_session_middleware
from .api.errors import install_error_handlers, not_found
from .config import Settings, get_settings
from .simulation import create_simulator
from .store.artifacts import ArtifactStore
from .store.db import DocumentStore

log = logging.getLogger("resonance")

FEATURE_MODULES = (
    "resonance.explore.service",
    "resonance.challenges.service",
    "resonance.agent.service",
)


def _feature_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for name in FEATURE_MODULES:
        try:
            modules.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    return modules


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.resonance_data_dir.mkdir(parents=True, exist_ok=True)
        store = DocumentStore(settings.db_path)
        app.state.settings = settings
        app.state.store = store
        app.state.artifacts = ArtifactStore(
            settings.artifacts_dir,
            store,
            max_count=settings.artifact_max_count,
            max_bytes=settings.artifact_max_mb * 1024 * 1024,
            max_per_session=settings.artifact_max_per_session,
        )
        app.state.simulator = create_simulator(settings)
        modules = _feature_modules()
        for mod in modules:
            hook = getattr(mod, "startup", None)
            if hook is not None:
                await hook(app)
        try:
            yield
        finally:
            for mod in reversed(modules):
                hook = getattr(mod, "shutdown", None)
                if hook is not None:
                    await hook(app)
            store.close()

    app = FastAPI(
        title="Resonance API",
        version=__version__,
        summary="RC circuit lab: ngspice simulation, honest audio, hidden-fault investigations.",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    install_session_middleware(app, settings)
    for router in (
        routes_meta.router,
        routes_revisions.router,
        routes_experiments.router,
        routes_cases.router,
        routes_agent.router,
        routes_learn.router,
    ):
        app.include_router(router)
    _mount_web(app, settings.web_dist_dir)
    return app


def _mount_web(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA (if present) with index.html fallback for client routes."""
    index = dist / "index.html"
    if not index.is_file():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise not_found("Endpoint")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        if "." in path.rsplit("/", 1)[-1]:
            # A missing static file (e.g. favicon.ico) is a 404, not the app shell.
            raise not_found("File")
        return FileResponse(index)


app = create_app()
