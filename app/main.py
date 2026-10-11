"""FastAPI app: Live RFQ (draft by chat, finalise, send, vendor upload link) and Compare quotes.

Run:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import base64
import importlib
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .live_rfx import smtp_settings

ROUTER_MODULES = ["live_rfx", "live_compare", "live_analyst"]

app = FastAPI(title="AerChain Sourcing")

# Buyer login. Off unless AERCHAIN_BUYER_PASSWORD is set (fine on your own machine). Vendors never log in: the upload
# page and its endpoint are open, protected only by the random link token.
OPEN_PREFIXES = ("/q/", "/api/q/")


@app.middleware("http")
async def buyer_login(request: Request, call_next):
    password = os.environ.get("AERCHAIN_BUYER_PASSWORD")
    if password and not request.url.path.startswith(OPEN_PREFIXES):
        sent = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                sent = base64.b64decode(header[6:]).decode("utf-8", "ignore").partition(":")[2]
            except Exception:
                sent = ""
        if not secrets.compare_digest(sent.encode(), password.encode()):
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="AerChain Sourcing", charset="UTF-8"'})
    return await call_next(request)

for _name in ROUTER_MODULES:
    app.include_router(importlib.import_module(f"app.{_name}").router)


@app.get("/api/health")
def health():
    return {"ok": True, "model": config.MODEL, "email_ready": smtp_settings() is not None,
            "catalog_ready": config.CATALOG.exists(), "openai_key": bool(config.openai_api_key())}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/web/#/live")


app.mount("/web", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
