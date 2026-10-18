"""FastAPI dependencies and anonymous session handling (owner: lead).

Feature packages read shared singletons from ``app.state`` through these functions so tests
can override them with ``app.dependency_overrides``.
"""

from __future__ import annotations

import re
import secrets
from http.cookies import SimpleCookie

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from ..contracts.internal import Simulator
from ..store.artifacts import ArtifactStore
from ..store.db import DocumentStore

SESSION_COOKIE = "rs_session"
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class SessionCookieMiddleware:
    """Pure ASGI session middleware.

    It must not be a ``BaseHTTPMiddleware`` (``@app.middleware("http")``): that wrapper hides
    client disconnects from ``Request.is_disconnected()``, so cancelling a request would not stop
    the simulation (verification finding B-01). ``receive`` is passed through untouched.
    """

    def __init__(self, app: ASGIApp, *, secure: bool) -> None:
        self.app = app
        self.secure = secure

    def _cookie_header(self, sid: str) -> str:
        cookie: SimpleCookie = SimpleCookie()
        cookie[SESSION_COOKIE] = sid
        morsel = cookie[SESSION_COOKIE]
        morsel["httponly"] = True
        morsel["samesite"] = "lax"
        morsel["max-age"] = 60 * 60 * 24 * 30
        morsel["path"] = "/"
        if self.secure:
            morsel["secure"] = True
        return morsel.OutputString()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sid = HTTPConnection(scope).cookies.get(SESSION_COOKIE)
        fresh = not (sid and _SESSION_RE.fullmatch(sid))
        if fresh or sid is None:
            sid = secrets.token_urlsafe(32)
        scope.setdefault("state", {})["session_id"] = sid
        if not fresh:
            await self.app(scope, receive, send)
            return
        header = self._cookie_header(sid)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", header)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def install_session_middleware(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(SessionCookieMiddleware, secure=settings.session_cookie_secure)


def get_session_id(request: Request) -> str:
    sid: str = request.state.session_id
    return sid


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_store(request: Request) -> DocumentStore:
    store: DocumentStore = request.app.state.store
    return store


def get_artifacts(request: Request) -> ArtifactStore:
    artifacts: ArtifactStore = request.app.state.artifacts
    return artifacts


def get_simulator(request: Request) -> Simulator:
    simulator: Simulator = request.app.state.simulator
    return simulator
