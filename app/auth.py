"""Sign-in for the buyer pages: one shared password, a login page, a long-lived session cookie and sign-out.

Off unless AERCHAIN_BUYER_PASSWORD is set (fine on your own machine). Vendors never sign in: `/q/<token>` and its upload
endpoint stay open, protected only by the random link token. The static app files under /web/ are open too: they hold no
data (the code is public) and serving them lets the page read the full URL, including the `#/...` part, before it asks
the server whether you are signed in.

Sessions: a random 256-bit token in an HttpOnly cookie; the server keeps only its hash (state/sessions.json), so
sessions survive a restart and a copied state file can't be replayed. Changing the password signs everyone out.
There is no idle timeout: you stay signed in until you sign out.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import config
from .util import iso
from . import visits

router = APIRouter()
COOKIE = "procurement_session"
MAX_AGE = 365 * 24 * 3600
OPEN_EXACT = {"/api/visit/beacon", "/login", "/web", "/api/health", "/api/auth/login", "/api/auth/logout"}
OPEN_PREFIXES = ("/q/", "/api/q/", "/web/")
_lock = threading.Lock()


def password() -> Optional[str]:
    return os.environ.get("AERCHAIN_BUYER_PASSWORD") or None


def _pw_stamp(pw: str) -> str:
    return hashlib.sha256(("procurement:" + pw).encode()).hexdigest()[:16]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _file():
    return config.STATE_DIR / "sessions.json"


def _load() -> dict:
    f = _file()
    return json.loads(f.read_text()) if f.exists() else {}


def _save(data: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)                                  # hashes only, but no reason for other users to read them
    tmp.replace(_file())


def new_session(pw: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        data[_hash(token)] = {"created": iso(config.real_now()), "pw": _pw_stamp(pw)}
        _save(data)
    return token


def valid_session(token: Optional[str]) -> bool:
    pw = password()
    if not pw or not token:
        return False
    rec = _load().get(_hash(token))
    return bool(rec) and secrets.compare_digest(rec["pw"], _pw_stamp(pw))        # a changed password invalidates old sessions


def end_session(token: Optional[str]) -> None:
    if token:
        with _lock:
            data = _load()
            if data.pop(_hash(token), None) is not None:
                _save(data)


def safe_next(n: Optional[str]) -> str:
    """Only a path on this site. Anything else (another host, a scheme, `//host`) goes to the app's home page."""
    n = (n or "").strip()
    if n.startswith("/") and not n.startswith("//") and "\\" not in n and "://" not in n and not urlparse(n).netloc:
        return n
    return "/web/#/live"


def _log(event: str, request: Request) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
        with (config.STATE_DIR / "auth.log").open("a") as f:
            f.write(f"{iso(config.real_now())}\t{event}\t{ip}\n")             # never the password
    except OSError:
        pass


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


async def guard(request: Request, call_next):
    """Middleware: block every buyer route without a valid session."""
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS"):                      # refuse forged requests from other sites
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    if not password() or path in OPEN_EXACT or path.startswith(OPEN_PREFIXES):
        return await call_next(request)
    token = request.cookies.get(COOKIE)
    if valid_session(token):
        return await call_next(request)
    if path == "/":
        return RedirectResponse("/login")
    resp = JSONResponse({"detail": "Sign in required", "reason": "expired" if token else "missing"}, status_code=401)
    if token:
        resp.delete_cookie(COOKIE, path="/")
    return resp


class LoginIn(BaseModel):
    password: str = ""
    next: Optional[str] = None


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if password() and valid_session(request.cookies.get(COOKIE)):
        return RedirectResponse("/")
    return FileResponse(config.WEB_DIR / "login.html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                               "Referrer-Policy": "same-origin"})


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    pw = password()
    if not pw:                                                                 # login not configured: nothing to sign in to
        return {"ok": True, "next": safe_next(body.next)}
    if not body.password:
        raise HTTPException(400, "Enter your password.")
    if not secrets.compare_digest(body.password.encode(), pw.encode()):
        _log("login_failed", request)
        visits.note("login_failed", request)
        raise HTTPException(401, "That password isn't right.")
    old = request.cookies.get(COOKIE)
    end_session(old)                                                           # a fresh token on every sign-in
    resp = JSONResponse({"ok": True, "next": safe_next(body.next)})
    resp.set_cookie(COOKIE, new_session(pw), max_age=MAX_AGE, httponly=True, samesite="lax", secure=_https(request), path="/")
    _log("login_ok", request)
    visits.note("login_ok", request)
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    end_session(request.cookies.get(COOKIE))
    _log("logout", request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/auth/me")
def me():
    """Reaching this far means the session is valid (or login is off). Tells the page whether to show Sign out."""
    return {"signed_in": True, "login_required": bool(password())}
