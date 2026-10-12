"""Visitor alerts: know when someone opens the site, and from where.

Tracked: the sign-in page, the app, vendor upload pages (with which RFQ and recipient the link belongs to), and every
sign-in success or failure. Each is logged to state/visits.jsonl. An email goes to AERCHAIN_VISIT_ALERT_TO (set on the
server, never in the repository) with the IP address, reverse DNS, browser, OS, device, language and referrer; a small
script on the page adds screen size, time zone, hardware and connection details.

Kept quiet on purpose: bots are logged but not emailed; one email per visitor and event per 30 minutes (sign-in failures
per 10 minutes); at most MAX_PER_HOUR emails an hour, with a note of how many were held back. Passwords, session cookies
and vendor link tokens are never logged or emailed.
"""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import smtplib
import socket
import threading
import time
from email.message import EmailMessage
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import config
from .util import iso

router = APIRouter()
DEBOUNCE = {"login_failed": 600}
DEFAULT_DEBOUNCE = 1800
MAX_PER_HOUR = 20
BEACON_WAIT = 4.0
BOT_RE = re.compile(r"bot|crawl|spider|slurp|curl|wget|python-requests|python-urllib|httpx|go-http|scrapy|libwww|zgrab|masscan|nmap|nikto|"
                    r"censys|shodan|lighthouse|pingdom|uptime|monitor", re.I)

_lock = threading.Lock()
_pending: dict[tuple, dict] = {}
_last_sent: dict[tuple, float] = {}
_sent_times: collections.deque = collections.deque()
_held_back = 0
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_dns_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def client_ip(request: Request) -> str:
    """The real address as nginx passes it (the app only listens on the Docker bridge, so the header can be trusted)."""
    h = request.headers
    return h.get("x-real-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "?")


def parse_ua(ua: str) -> dict:
    ua = ua or ""
    browser = next((n for n, rx in (("Edge", r"Edg/"), ("Opera", r"OPR/"), ("Firefox", r"Firefox/"), ("Chrome", r"(Chrome|CriOS)/"),
                                    ("Safari", r"Safari/"), ("curl", r"curl/"), ("Python", r"python")) if re.search(rx, ua, re.I)), "unknown")
    m = re.search(r"(?:Chrome|Firefox|Edg|OPR|Version|CriOS)/(\d+)", ua)
    os_ = next((n for n, rx in (("iOS", r"iPhone|iPad|iPod"), ("Android", r"Android"), ("Windows", r"Windows NT"),
                                ("macOS", r"Mac OS X|Macintosh"), ("ChromeOS", r"CrOS"), ("Linux", r"Linux|X11")) if re.search(rx, ua)), "unknown")
    device = "tablet" if re.search(r"iPad|Tablet", ua) else "mobile" if re.search(r"Mobile|iPhone", ua) else "desktop"
    return {"browser": browser + (f" {m.group(1)}" if m else ""), "os": os_, "device": device,
            "bot": bool(BOT_RE.search(ua)) or not ua, "automation": "HeadlessChrome" in ua}


def mask(path: str) -> str:
    """Never keep a vendor link token: /q/AbCdEfGh... becomes /q/AbCd…"""
    return re.sub(r"^(/q/)([^/?#]{4})[^/?#]*", r"\1\2…", path)


def vendor_link_info(path: str) -> Optional[dict]:
    m = re.match(r"^/q/([^/?#]+)", path)
    if not m:
        return None
    try:
        from . import live_rfx as L
        st, email = L._by_token(m.group(1))
        return {"rfq": st["id"], "scope": str((st.get("rfx") or {}).get("scope") or "")[:80],
                "link": f"emailed to {email}" if email else "the shared link"}
    except Exception:
        return {"rfq": "unknown link"}


def reverse_dns(ip: str) -> str:
    try:
        return _dns_pool.submit(socket.gethostbyaddr, ip).result(timeout=3)[0]
    except Exception:
        return "(no reverse DNS)"


def note(event: str, request: Request, extra: Optional[dict] = None) -> None:
    """Record a tracked event. Returns at once; the log write and the email happen in the background."""
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    if ip in {x.strip() for x in os.environ.get("AERCHAIN_VISIT_IGNORE_IPS", "").split(",") if x.strip()}:
        return
    h = request.headers
    data = {"time": iso(config.real_now()), "event": event, "ip": ip, "forwarded_for": h.get("x-forwarded-for"), "host": h.get("host"),
            "path": mask(request.url.path), "user_agent": ua[:400], **parse_ua(ua), "language": h.get("accept-language", "")[:80],
            "referrer": (h.get("referer") or "")[:200] or None, "dnt": h.get("dnt"),
            "client_hints": {k: h.get(k) for k in ("sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile", "sec-ch-ua-model") if h.get(k)} or None,
            "fetch_site": h.get("sec-fetch-site"), "vendor_link": vendor_link_info(request.url.path), **(extra or {})}
    key = (ip, ua)
    with _lock:
        if key in _pending:                                   # several tracked events at once: one email
            _pending[key]["events"].append(event)
            _pending[key]["data"]["event"] = event
            return
        entry = {"data": data, "events": [event], "beacon": None, "timer": threading.Timer(BEACON_WAIT, flush, args=[key])}
        entry["timer"].daemon = True
        _pending[key] = entry
        entry["timer"].start()


class Beacon(BaseModel):
    screen: str = ""
    viewport: str = ""
    dpr: float = 0
    timezone: str = ""
    languages: str = ""
    platform: str = ""
    cores: int = 0
    memory_gb: float = 0
    touch_points: int = 0
    connection: str = ""
    cookies: bool = True
    webdriver: bool = False


@router.post("/api/visit/beacon")
def beacon(b: Beacon, request: Request):
    """Device details the browser knows, merged into the visit just recorded for this address and browser; ignored otherwise."""
    key = (client_ip(request), request.headers.get("user-agent", ""))
    with _lock:
        entry = _pending.get(key)
        if entry and entry["beacon"] is None:
            entry["beacon"] = {k: (v[:120] if isinstance(v, str) else v) for k, v in b.model_dump().items() if v not in ("", 0, None)}
            entry["timer"].cancel()
            threading.Thread(target=flush, args=[key], daemon=True).start()
    return {"ok": True}


def flush(key: tuple) -> None:
    global _held_back
    with _lock:
        entry = _pending.pop(key, None)
    if not entry:
        return
    d = entry["data"]
    d["events"] = entry["events"]
    d["device_info"] = entry["beacon"]
    d["reverse_dns"] = reverse_dns(d["ip"])
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (config.STATE_DIR / "visits.jsonl").open("a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except OSError:
        pass
    to = os.environ.get("AERCHAIN_VISIT_ALERT_TO")
    important = any(e in ("login_failed", "login_ok") for e in d["events"])
    if not to or (d["bot"] and not important):
        return
    stamp = (d["ip"], hashlib.sha256(d["user_agent"].encode()).hexdigest()[:10], d["event"])
    now = time.time()
    with _lock:
        if now - _last_sent.get(stamp, 0) < DEBOUNCE.get(d["event"], DEFAULT_DEBOUNCE):
            return
        while _sent_times and now - _sent_times[0] > 3600:
            _sent_times.popleft()
        if len(_sent_times) >= MAX_PER_HOUR:
            _held_back += 1
            return
        _last_sent[stamp] = now
        _sent_times.append(now)
        held, _held_back = _held_back, 0
    _pool.submit(send_alert, to, d, held, count_for(d["ip"]))


def count_for(ip: str) -> int:
    try:
        return sum(1 for line in (config.STATE_DIR / "visits.jsonl").read_text().splitlines()[-5000:] if json.loads(line).get("ip") == ip)
    except Exception:
        return 1


TITLES = {"login_page": "opened the sign-in page", "app_open": "opened the app", "vendor_page": "opened a vendor upload page",
          "login_ok": "signed in", "login_failed": "FAILED to sign in"}


def render(d: dict, held: int, seen: int) -> tuple[str, str]:
    b = d.get("device_info") or {}
    vl = d.get("vendor_link")
    rows = [("When", d["time"]), ("What", ", ".join(TITLES.get(e, e) for e in d["events"])), ("Page", d["path"]),
            ("IP address", d["ip"]), ("Reverse DNS", d["reverse_dns"]), ("Forwarded chain", d.get("forwarded_for") or "-"),
            ("Logged visits from this IP", str(seen)), ("Browser", d["browser"]), ("Operating system", d["os"]),
            ("Device type", d["device"] + (" (looks automated)" if d["bot"] or d["automation"] else "")),
            ("Language", d["language"] or "-"), ("Referrer", d["referrer"] or "(direct)"), ("Do Not Track", d.get("dnt") or "-"),
            ("User agent", d["user_agent"])]
    if vl:
        rows.insert(3, ("Vendor link", f"{vl.get('rfq')} | {vl.get('link', '')} | {vl.get('scope', '')}"))
    if d.get("client_hints"):
        rows.append(("Client hints", json.dumps(d["client_hints"])))
    if b:
        rows += [("Screen", b.get("screen", "-")), ("Window", b.get("viewport", "-")), ("Pixel ratio", str(b.get("dpr", "-"))),
                 ("Time zone", b.get("timezone", "-")), ("Browser languages", b.get("languages", "-")), ("Platform", b.get("platform", "-")),
                 ("CPU cores", str(b.get("cores", "-"))), ("Memory (GB, approx)", str(b.get("memory_gb", "-"))),
                 ("Touch points", str(b.get("touch_points", "-"))), ("Connection", b.get("connection", "-")),
                 ("Cookies enabled", str(b.get("cookies", "-"))), ("Automation flag", str(b.get("webdriver", False)))]
    else:
        rows.append(("Device details", "(the page script did not report: blocked, a bot, or a very quick navigation)"))
    body = "\n".join(f"{k:<28}{v}" for k, v in rows)
    if held:
        body += f"\n\n{held} more alert(s) were held back in the last hour (cap {MAX_PER_HOUR} an hour). All are in state/visits.jsonl."
    body += "\n\nSent by the Procurement app. Stop these alerts by removing AERCHAIN_VISIT_ALERT_TO on the server."
    return f"Procurement: {TITLES.get(d['event'], d['event'])} | {d['ip']}", body


def send_alert(to: str, d: dict, held: int, seen: int) -> None:
    try:
        from .live_rfx import smtp_settings
        cfg = smtp_settings()
        if not cfg:
            return
        subject, body = render(d, held, seen)
        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = cfg["SMTP_FROM"] or cfg["SMTP_USER"], to, subject
        em.set_content(body)
        with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"] or 587), timeout=30) as s:
            s.starttls()
            s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            s.send_message(em)
    except Exception as e:                       # an alert must never break the site
        try:
            with (config.STATE_DIR / "visits.errors.log").open("a") as f:
                f.write(f"{iso(config.real_now())}\t{type(e).__name__}: {str(e)[:200]}\n")
        except OSError:
            pass


TRACKED = {"/login": "login_page", "/web/": "app_open", "/web/index.html": "app_open"}


async def track(request: Request, call_next):
    resp = await call_next(request)
    try:
        if request.method == "GET" and resp.status_code == 200:
            ev = TRACKED.get(request.url.path) or ("vendor_page" if request.url.path.startswith("/q/") else None)
            if ev:
                note(ev, request)
    except Exception:
        pass
    return resp
