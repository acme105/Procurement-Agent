"""FastAPI app: Live RFQ (draft by chat, finalise, send, vendor upload link) and Compare quotes.

Run:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .live_rfx import smtp_settings

ROUTER_MODULES = ["live_rfx", "live_compare", "live_analyst"]

app = FastAPI(title="AerChain Sourcing")

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
