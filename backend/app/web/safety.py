"""Part 6b: the buttons have no login (the app runs on your laptop only), so two guards stop another website
from pressing them through your browser:

1. Every request must be addressed to this laptop (localhost, 127.0.0.1, or the PUBLIC_BASE_URL host). A site
   that points its own name at your laptop ("DNS rebinding") is refused here.
2. Anything that changes something (POST, PUT, DELETE…) coming from a page of another site is refused — the
   browser says where a request comes from (Origin / Referer). The terminal and curl send neither: allowed.

On top of that, each button carries a secret code that only the agent's own pages have (`form_token`); a press
without it is refused (`check_token`). The code lives in a cookie your browser never sends to other sites.
"""

import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.settings import get_settings

TOKEN_COOKIE = "ja_form"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def allowed_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}          # (pytest adds its own client's name: tests/conftest.py)
    own = urlsplit(get_settings().public_base_url).hostname
    if own:
        hosts.add(own.lower())
    return hosts


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


class Guard:
    """Plain ASGI middleware (no buffering, so streamed responses still stream)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if _hostname(host) not in allowed_hosts():
            await PlainTextResponse("This app only answers on localhost.", status_code=400)(scope, receive, send)
            return
        if scope.get("method", "GET") not in SAFE_METHODS:
            source = headers.get("origin") or headers.get("referer")
            if source and source != "null" and urlsplit(source).netloc.lower() != host.lower():
                await PlainTextResponse("Refused: this button wasn't pressed on the agent's own page.",
                                        status_code=403)(scope, receive, send)
                return
            if source == "null":                                     # sandboxed / file:// pages
                await PlainTextResponse("Refused: unknown page.", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def form_token(request: Request) -> str:
    """This browser's secret code (made on the first visit; `remember_token` stores it in the cookie)."""
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        token = getattr(request.state, "new_token", None) or secrets.token_urlsafe(32)
        request.state.new_token = token
    return token


def remember_token(request: Request, response) -> None:
    new = getattr(request.state, "new_token", None)
    if new:
        # Lax, not Strict: arriving from the summary mail must not replace the code your other tabs use (the
        # Origin check above already refuses presses from other sites)
        response.set_cookie(TOKEN_COOKIE, new, httponly=True, samesite="lax", max_age=400 * 86400, path="/")


def check_token(request: Request, token: str) -> None:
    expected = request.cookies.get(TOKEN_COOKIE) or ""
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(403, "This button's code is missing or old. Reload the page and press it again.")


def safe_next(path: str | None, fallback: str = "/jobs") -> str:
    """Where to go after a button: only a page of this app (never another site)."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return fallback
