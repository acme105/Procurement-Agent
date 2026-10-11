"""Live RFQ builder: the guided co-pilot from rfx_copilot.ipynb as an API.

The buyer chats with 'Ana'; every tool call streams the updated RFQ (Server-Sent Events) so the page redraws live.
Finalise locks the RFQ and prints one PDF; only then can it be emailed (Gmail SMTP) or downloaded.
State is one JSON file under STATE_DIR/live. Settings: ~/.config/aerchain/smtp.env or SMTP_* env vars.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import smtplib
import threading
import urllib.parse
import urllib.request
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import config, llm, printing
from .util import iso

router = APIRouter()
BRAND = "Procurement"
LOGO_SVG = "<svg viewBox='0 0 40 40' xmlns='http://www.w3.org/2000/svg'> <defs><linearGradient id='pg' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#3B03C5'/><stop offset='1' stop-color='#9B38FC'/></linearGradient></defs> <rect width='40' height='40' rx='10' fill='url(#pg)'/> <path d='M13 25.5V9.5h8.4a5.2 5.2 0 0 1 0 10.4H13' fill='none' stroke='#fff' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/> <path d='M20.5 29.6l3.2 3.2 6.6-7.2' fill='none' stroke='#47DEFF' stroke-width='3.2' stroke-linecap='round' stroke-linejoin='round'/> </svg>"
LIVE_DIR = config.STATE_DIR / "live"
RFQ_DIR = LIVE_DIR / "rfqs"                     # one folder per RFQ: state.json, rfq.pdf, uploads/
CURRENT_FILE = LIVE_DIR / "current.txt"
MAX_UPLOAD = 20 * 1024 * 1024
UPLOAD_EXT = {".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt"}
CATALOG = config.CATALOG
_lock = threading.Lock()
_df = None


def catalog() -> pd.DataFrame:
    global _df
    if _df is None:
        _df = pd.read_csv(CATALOG)
    return _df


def search_catalog(keywords=None, category=None):
    """Searches the catalog and returns matching SKUs across all categories."""
    results = catalog().copy()
    
    if category:
        results = results[results['category'].str.contains(category, case=False, na=False)]
        
    if keywords:
        for kw in keywords:
            # Strip trailing 's' for basic singular matching (e.g., 'laptops' -> 'laptop')
            search_kw = kw[:-1] if kw.lower().endswith('s') and len(kw) > 3 else kw
            
            # Search across description, category, and sub_category
            mask = (
                results['description'].str.contains(search_kw, case=False, na=False) |
                results['category'].str.contains(search_kw, case=False, na=False) |
                results['sub_category'].str.contains(search_kw, case=False, na=False)
            )
            results = results[mask]
            if results.empty:
                return []
                
    # Return top 15 matches to keep the LLM context clean
    return results[['item_code', 'description', 'category', 'sub_category', 'status']].head(50).to_dict('records')

def search_internet(query):
    """Searches Wikipedia for hardware specs or general knowledge."""
    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&utf8=&format=json"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            results = data.get('query', {}).get('search', [])
            return [{"title": r["title"], "snippet": r["snippet"]} for r in results[:3]]
    except Exception as e:
        return f"Error searching Wikipedia: {e}"



SYSTEM_PROMPT = """You are 'Ana', a helpful, efficient procurement copilot. Your persona is sharp, friendly, and concise. You ALWAYS respond in very short messages (1-3 sentences max).
Your job is to actively guide the user to build a complete RFx step-by-step. Do not let them just search and leave. Guide them through these steps, one at a time:
1. Scope: Ask what the overarching goal of the procurement is. Use set_rfx_scope to save it.
2. Line Items: Help them search the catalog. Use add_to_rfx to save items.
   *DRILL DOWN GUIDANCE*: If the user searches for a broad category and you find many results, DO NOT just list SKUs. Summarize the broad *types* available (e.g. "We have Ergonomic and Gaming mice. Which do you prefer?") to help them drill down.
   *CLARIFYING SPECS*: If a user's request for complex IT hardware (like a laptop) is underspecified, DO NOT immediately call add_to_rfx. Ask follow-up questions to clarify the exact specs (e.g., "Do you need M2 or M3 chips?", "What screen size?") BEFORE saving it.
   *NO QUANTITIES IN DESCRIPTIONS*: NEVER include the numerical quantity in the `generic_requirements` field. The quantity belongs ONLY in the `site_quantities` argument. The generic requirement should only describe the item (e.g., "MacBook Pro, 32GB RAM", NOT "10 MacBook Pros").
   *INTERNET SEARCH*: If the user asks a technical or domain-specific question (e.g., "Does the M3 Max have a GPU?"), use the search_internet tool to gather factual information before answering.
3. Questionnaire: Ask them if they want to include a questionnaire for vendors. Use set_questionnaire to save their questions. Note that set_questionnaire COMPLETELY OVERWRITES the previous list, so if the user asks you to modify a question, rewrite the entire list and pass it in.
Important rules:
- NEVER add an item, or a quantity, that the buyer has not stated themselves. Do not add items "to be helpful", from your own suggestions, or with a quantity you assumed. If something is unclear, ask. The system refuses an add whose item or quantity does not appear in the buyer's messages.
- Remind them of what is remaining if they miss a step.
- Finish ONE STEP at a time.
- Keep your answers very short and conversational.
"""

tools = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": "Search the item master catalog. IMPORTANT: ONLY pass the primary noun (e.g. 'MacBook Pro', 'Laptop', 'Mouse') in keywords. Do NOT pass specific specs like '32GB' or 'GPU' into the search tool; instead, search for the broad category and filter the results yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords to search"},
                    "category": {"type": "string", "description": "Optional broad category filter"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_rfx_scope",
            "description": "Set the overarching project scope or title for the RFx (e.g., 'IT Hardware Refresh 2026').",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "The RFx project title or purpose"}
                },
                "required": ["scope"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_rfx",
            "description": "Add specific item codes OR a generic requirement to the RFx draft, including site quantities. Every item code becomes its own line item (you may pass several codes in one call). An item with no catalog code is added as generic_requirements, one line per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_codes": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "description": "Specific item codes (e.g. IT-LAP-0001) to add."
                    },
                    "generic_requirements": {
                        "type": "string",
                        "description": "Description of minimum/maximum acceptable limits if the user doesn't pick an exact SKU (e.g., 'Any 14-inch laptop with i7 and 16GB RAM')."
                    },
                    "site_quantities": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                        "description": "Quantities needed per site (e.g., {'Hosur': 100, 'Bangalore': 50})."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_questionnaire",
            "description": "Sets or replaces the entire list of questions for suppliers to answer. This COMPLETELY OVERWRITES the previous list, so always provide the FULL list of desired questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "description": "Full list of questions to ask suppliers."
                    }
                },
                "required": ["questions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_rfx",
            "description": "Remove one or more specific line items from the RFx draft by their 1-based line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_numbers": {"type": "array", "items": {"type": "integer"}, "description": "The 1-based line item numbers to remove (e.g. [1, 2, 5])."}
                },
                "required": ["line_numbers"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_internet",
            "description": "Search the internet (Wikipedia) to answer domain-specific or hardware questions (e.g. \"Does M3 Max have a GPU?\").",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"]
            }
        }
    }
]


# ---- state: one folder per RFQ ------------------------------------------------------------------

def _fresh(rid: str) -> dict:
    return {"id": rid, "rfx": {"scope": None, "items": [], "questionnaire": []}, "messages": [], "chat": [],
            "status": "DRAFT", "created_at": iso(config.real_now()), "finalised_at": None, "sent": [], "links": {}, "uploads": []}


def rdir(rid: str) -> Path:
    if not re.fullmatch(r"RFQ-L-\d{3,}", rid):
        raise HTTPException(404, "Unknown RFQ")
    return RFQ_DIR / rid


def load(rid: str) -> dict:
    f = rdir(rid) / "state.json"
    if not f.exists():
        raise HTTPException(404, "Unknown RFQ")
    return json.loads(f.read_text())


def save(st: dict) -> None:
    d = rdir(st["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))


def all_rfqs() -> list[dict]:
    out = []
    for f in sorted(RFQ_DIR.glob("RFQ-L-*/state.json")) if RFQ_DIR.exists() else []:
        out.append(json.loads(f.read_text()))
    return out


def is_empty(st: dict) -> bool:
    return st["status"] == "DRAFT" and not st["rfx"]["scope"] and not st["rfx"]["items"] and not st["chat"]


def create_rfq() -> dict:
    n = max([int(s["id"].split("-")[2]) for s in all_rfqs()], default=0) + 1
    st = _fresh(f"RFQ-L-{n:03d}")
    save(st)
    return st


def set_current(rid: str) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(rid)


def current() -> dict:
    rid = CURRENT_FILE.read_text().strip() if CURRENT_FILE.exists() else ""
    if rid and (RFQ_DIR / rid / "state.json").exists():
        return load(rid)
    st = create_rfq()
    set_current(st["id"])
    return st


def describe(item: dict) -> str:
    parts = []
    if item.get("item_codes"):
        d = catalog()
        parts += d[d["item_code"].isin(item["item_codes"])]["description"].tolist()
    if item.get("generic_requirements"):
        parts.append(item["generic_requirements"])
    return " | ".join(parts) or "N/A"


def public_base(request: Request) -> str:
    return os.environ.get("AERCHAIN_PUBLIC_URL", str(request.base_url)).rstrip("/")


def view(st: dict, request: Optional[Request] = None) -> dict:
    """What the page shows: the RFQ with resolved descriptions, plus chat, status, send log and replies."""
    r = st["rfx"]
    lines = [{"n": i + 1, "description": describe(it), "codes": it.get("item_codes", []),
              "location": it.get("location", "N/A"), "quantity": it.get("quantity", "N/A")} for i, it in enumerate(r["items"])]
    out = {"id": st["id"], "scope": r["scope"], "lines": lines, "questionnaire": r["questionnaire"], "chat": st["chat"],
           "status": st["status"], "created_at": st["created_at"], "finalised_at": st["finalised_at"], "sent": st["sent"],
           "uploads": [{"n": u["n"], "org": u.get("org") or u.get("email") or "Unknown", "filename": u["filename"], "size": u["size"], "at": u["at"],
                    "source": u.get("source") or ("email" if u.get("email") else "link")}
                   for u in st["uploads"] if not u.get("superseded")],
           "smtp_ready": smtp_settings() is not None}
    if request is not None:
        base = public_base(request)
        if st["status"] == "FINAL":
            if not st.get("share_token"):          # older finalised RFQs get their shareable link on first view
                st["share_token"] = secrets.token_urlsafe(16)
                save(st)
            out["share_url"] = f"{base}/q/{st['share_token']}"
        out["local_only"] = bool(re.match(r"https?://(localhost|127\.|0\.0\.0\.0)", base))
    return out


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
              "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "dozen": 12}
_STOP = {"with", "from", "inch", "core", "unit", "units", "each", "item", "items", "type", "types", "and", "the", "for", "per", "pcs", "piece", "pieces", "ram", "ssd", "series", "model"}


def _tokens(text: str) -> set[str]:
    """Significant words, with plurals folded ("laptops" is "laptop", "mice" is "mouse")."""
    out = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        t = "mouse" if t == "mice" else (t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t)
        if (len(t) >= 3 and t not in _STOP) or t.isdigit():
            out.add(t)
    return out


def ungrounded(st: dict, args: dict) -> Optional[str]:
    """Why an add_to_rfx call can't be accepted: the item or quantity never came from the buyer. None if it is grounded."""
    chat = st.get("chat") or []
    said = " ".join(m["text"] for m in chat if m.get("role") == "user")
    discussed = said + " " + " ".join(m["text"] for m in chat if m.get("role") == "assistant")         # options Ana listed and the buyer picked
    said_numbers = {int(n) for n in re.findall(r"(?<![\d.])(\d+)(?![\d.])", said)} | {v for w, v in _NUM_WORDS.items() if re.search(rf"\b{w}\b", said.lower())}
    for site, q in (args.get("site_quantities") or {}).items():
        try:
            if int(q) not in said_numbers:
                return f"The quantity {q} for '{site}' was not given by the buyer. Ask them for the quantity instead of assuming one."
        except (TypeError, ValueError):
            return f"The quantity '{q}' is not a number. Ask the buyer."
    words = _tokens(discussed)
    texts = []
    if args.get("generic_requirements"):
        texts.append(("the requirement", args["generic_requirements"]))
    codes = args.get("item_codes") or []
    if codes:
        d = catalog()
        for code in codes:
            row = d[d["item_code"] == code]
            texts.append((code, row["description"].iloc[0] if len(row) else ""))
    for label, text in texts:
        mine = _tokens(text)
        if mine and not (mine & words):
            return f"'{text[:60]}' ({label}) has not been mentioned by the buyer. Do not add items they did not ask for; ask them first."
    return None


def apply_tool(st: dict, name: str, args: dict) -> str:
    r = st["rfx"]
    if name == "add_to_rfx":
        why = ungrounded(st, args)
        if why:
            return "NOT ADDED. " + why
    if name == "search_catalog":
        return json.dumps(search_catalog(**args))
    if name == "search_internet":
        return json.dumps(search_internet(args.get("query", "")))
    if name == "set_rfx_scope":
        r["scope"] = args.get("scope")
        return "Scope saved successfully."
    if name == "set_questionnaire":
        r["questionnaire"] = args.get("questions", [])
        return "Questionnaire completely replaced/set successfully."
    if name == "add_to_rfx":
        start = len(r["items"])
        sq = args.get("site_quantities") or {}
        codes, generic = args.get("item_codes") or [], args.get("generic_requirements", "")
        # several catalog codes are several items: each gets its own line (a line holds many codes only alongside a requirement text)
        groups = [[code] for code in codes] if len(codes) > 1 and not generic else [codes]
        for group in groups:
            base = {"item_codes": group, "generic_requirements": generic}
            if not sq:
                r["items"].append({**base, "location": "N/A", "quantity": "N/A"})
            else:
                for site, q in sq.items():
                    r["items"].append({**base, "location": site, "quantity": q})
        return f"Items added successfully at Line Numbers: {list(range(start + 1, len(r['items']) + 1))}"
    if name == "remove_from_rfx":
        nums = args.get("line_numbers", [])
        nums = nums if isinstance(nums, list) else [nums]
        removed, errors = [], []
        for n in sorted(nums, reverse=True):
            if 0 < n <= len(r["items"]):
                r["items"].pop(n - 1)
                removed.append(n)
            else:
                errors.append(n)
        return f"Removed line items: {removed}." + (f" Errors (not found): {errors}." if errors else "")
    return f"Unknown tool {name}"


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"



# ---- routes: RFQs, history, chat ---------------------------------------------------------------

class MessageIn(BaseModel):
    text: str


@router.get("/api/live/current")
def get_current(request: Request):
    return view(current(), request)


@router.get("/api/live/rfqs")
def history():
    """Every RFQ except untouched empty drafts, newest first."""
    rows = []
    for st in reversed(all_rfqs()):
        if is_empty(st):
            continue
        rows.append({"id": st["id"], "scope": st["rfx"]["scope"], "status": st["status"], "lines": len(st["rfx"]["items"]),
                     "created_at": st["created_at"], "finalised_at": st["finalised_at"],
                     "sent": sum(1 for s in st["sent"] if s["status"] == "sent"), "replies": len({org_key(u.get("org") or u.get("email") or "") for u in st["uploads"] if not u.get("superseded")})})
    return rows


@router.post("/api/live/rfqs")
def new_rfq(request: Request):
    """Start a new RFQ. An untouched empty draft is reused, so clicking twice doesn't pile up blanks."""
    with _lock:
        cur = current()
        st = cur if is_empty(cur) else create_rfq()
        set_current(st["id"])
    return view(st, request)


@router.post("/api/live/rfqs/{rid}/open")
def open_rfq(rid: str, request: Request):
    st = load(rid)
    set_current(rid)
    return view(st, request)


@router.get("/api/live/rfqs/{rid}")
def get_rfq(rid: str, request: Request):
    return view(load(rid), request)


@router.post("/api/live/rfqs/{rid}/messages")
def post_message(rid: str, body: MessageIn, request: Request):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Write a message")
    st = load(rid)
    if st["status"] != "DRAFT":
        raise HTTPException(409, "This RFQ is finalised; start a new one to keep chatting")

    def stream():
        with _lock:
            msgs = st["messages"] or [{"role": "system", "content": SYSTEM_PROMPT}]
            msgs[0] = {"role": "system", "content": SYSTEM_PROMPT}      # always the current prompt, also for drafts started earlier
            msgs.append({"role": "user", "content": text})
            st["chat"].append({"role": "user", "text": text})
            yield _sse("rfx", view(st, request))
            try:
                while True:
                    resp = llm.client().chat.completions.create(model=config.MODEL, messages=msgs, tools=tools,
                                                                reasoning_effort="none")
                    m = resp.choices[0].message
                    msgs.append(m.model_dump(exclude_none=True))
                    if m.content:
                        st["chat"].append({"role": "assistant", "text": m.content})
                    if not m.tool_calls:
                        break
                    for tc in m.tool_calls:
                        yield _sse("tool", {"name": tc.function.name})
                        out = apply_tool(st, tc.function.name, json.loads(tc.function.arguments or "{}"))
                        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                        st["messages"] = msgs
                        save(st)
                        yield _sse("rfx", view(st, request))
            except Exception as e:  # model or network error: keep what was saved, tell the page
                st["chat"].append({"role": "assistant", "text": f"Sorry, I hit a problem: {str(e)[:200]}", "error": True})
            st["messages"] = msgs
            save(st)
            yield _sse("rfx", view(st, request))
            yield _sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---- routes: finalise, pdf, send ----------------------------------------------------------------

def rfq_html(v: dict) -> str:
    rows = "".join(f"<tr><td>{l['n']}</td><td>{escape(l['description'])}</td><td>{escape(str(l['location']))}</td>"
                   f"<td class='n'>{escape(str(l['quantity']))}</td></tr>" for l in v["lines"])
    qs = "".join(f"<li>{escape(q)}</li>" for q in v["questionnaire"]) or "<li class='m'>None</li>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Request for quotation</title>
<style>@page{{size:A4;margin:16mm}}body{{font:400 9.5pt/1.4 Helvetica,Arial,sans-serif;color:#242526}}
h1{{font:700 19pt/1.2 Helvetica,Arial,sans-serif;color:#100E5B;margin:4pt 0 2pt}}h2{{font:700 12pt Helvetica,Arial,sans-serif;color:#373192;margin:16pt 0 6pt}}
.m{{color:#5B6170}}table{{width:100%;border-collapse:collapse}}th{{text-align:left;background:#EEEDFB;color:#373192;font-weight:600}}
th,td{{padding:3pt 6pt;border-bottom:1px solid #E3E6EE;vertical-align:top}}tr{{page-break-inside:avoid}}.n{{text-align:right}}</style></head><body>
<div class="m" style="display:flex;align-items:center;gap:8pt"><span style="width:22pt;height:22pt;display:inline-block">{LOGO_SVG}</span><b style="color:#242526;font-size:11pt">{BRAND}</b> · Request for quotation</div><h1>{escape(v['scope'] or 'Request for quotation')}</h1>
<div class="m">Finalised {escape(v['finalised_at'] or '')}</div>
<h2>Line items</h2><table><thead><tr><th>#</th><th>Item</th><th>Location</th><th class="n">Qty</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Questions for suppliers</h2><ol>{qs}</ol></body></html>"""


@router.post("/api/live/rfqs/{rid}/finalise")
def finalise(rid: str, request: Request):
    with _lock:
        st = load(rid)
        if st["status"] == "FINAL":
            return view(st, request)
        if not st["rfx"]["scope"] or not st["rfx"]["items"]:
            raise HTTPException(422, "Add a scope and at least one line item first")
        st["status"], st["finalised_at"] = "FINAL", iso(config.real_now())
        st["share_token"] = secrets.token_urlsafe(16)
        try:
            printing.print_pdf(rfq_html(view(st)), rdir(rid) / "rfq.pdf")
        except printing.PdfError as e:
            raise HTTPException(503, f"The PDF couldn't be printed, nothing was finalised: {e}") from e
        save(st)
        return view(st, request)


def _need_final(rid: str) -> dict:
    st = load(rid)
    if st["status"] != "FINAL" or not (rdir(rid) / "rfq.pdf").exists():
        raise HTTPException(409, "Finalise the RFQ first")
    return st


@router.get("/api/live/rfqs/{rid}/pdf")
def get_pdf(rid: str):
    _need_final(rid)
    return FileResponse(rdir(rid) / "rfq.pdf", media_type="application/pdf", filename=f"{rid}.pdf")


def smtp_settings() -> Optional[dict]:
    import os
    vals = {}
    f = Path.home() / ".config" / "aerchain" / "smtp.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("'\"")
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
        vals[k] = os.environ.get(k, vals.get(k, ""))
    return vals if vals["SMTP_HOST"] and vals["SMTP_USER"] and vals["SMTP_PASS"] else None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendIn(BaseModel):
    emails: list[str]
    subject: str
    message: str


@router.post("/api/live/rfqs/{rid}/send")
def send(rid: str, body: SendIn, request: Request):
    st = _need_final(rid)
    cfg = smtp_settings()
    if not cfg:
        raise HTTPException(503, "Email isn't set up: add SMTP settings, or download the PDF and forward it yourself")
    emails = list(dict.fromkeys(e.strip().lower() for e in body.emails if e.strip()))
    bad = [e for e in emails if not EMAIL_RE.match(e)]
    if not emails or bad:
        raise HTTPException(422, f"Invalid email address: {', '.join(bad)}" if bad else "Add at least one email address")
    base = public_base(request)
    with _lock:
        st = load(rid)
        entries = []
        for to in emails:
            token = st["links"].get(to) or secrets.token_urlsafe(16)    # one link per recipient, reused on resend
            st["links"][to] = token
            entries.append({"id": secrets.token_hex(4), "email": to, "at": iso(config.real_now()), "status": "sending"})
        st["sent"] += entries
        save(st)
        links = dict(st["links"])
    # Gmail takes ~3s per message plus a ~5s handshake, so send in the background on a few connections
    # and let the page show each recipient's progress.
    threading.Thread(target=_deliver, args=(rid, entries, links, base, body.subject, body.message, cfg), daemon=True).start()
    return view(load(rid), request)


def _mark(rid: str, eid: str, status: str, error: str = "") -> None:
    with _lock:
        st = load(rid)
        for e in st["sent"]:
            if e.get("id") == eid:
                e["status"], e["at"] = status, iso(config.real_now())
                if error:
                    e["error"] = error
        save(st)
        (config.STATE_DIR / "outbox").mkdir(parents=True, exist_ok=True)
        with (config.STATE_DIR / "outbox" / "live-sent.log").open("a") as f:
            em = next((e["email"] for e in st["sent"] if e.get("id") == eid), "")
            f.write(f"{iso(config.real_now())}\t{rid}\t{em}\t{status}\n")


def _deliver(rid: str, entries: list, links: dict, base: str, subject: str, message: str, cfg: dict) -> None:
    pdf_bytes = (rdir(rid) / "rfq.pdf").read_bytes()
    chunks = [entries[i::3] for i in range(min(3, len(entries)))]      # up to 3 connections in parallel

    def run(chunk: list) -> None:
        try:
            with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"] or 587), timeout=30) as s:
                s.starttls()
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                for e in chunk:
                    em = EmailMessage()
                    em["From"], em["To"], em["Subject"] = cfg["SMTP_FROM"] or cfg["SMTP_USER"], e["email"], subject
                    em.set_content(f"{message}\n\nUpload your quotation here:\n{base}/q/{links[e['email']]}\n")
                    em.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=f"{rid}.pdf")
                    try:
                        s.send_message(em)
                        _mark(rid, e["id"], "sent")
                    except Exception as ex:
                        _mark(rid, e["id"], "failed", str(ex)[:160])
        except Exception as ex:      # connect or login failed: every recipient in this chunk failed
            for e in chunk:
                _mark(rid, e["id"], "failed", f"Couldn't reach the email server: {str(ex)[:120]}")

    threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---- vendor upload link (public, no login: the token is the key) ---------------------------------

def _by_token(token: str) -> tuple[dict, Optional[str]]:
    """The RFQ and, for a per-recipient link, the email it was sent to (None for the shared link)."""
    for st in all_rfqs():
        if st.get("share_token") and secrets.compare_digest(st["share_token"], token):
            return st, None
        for email, t in st["links"].items():
            if secrets.compare_digest(t, token):
                return st, email
    raise HTTPException(404, "This link isn't valid")


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


@router.get("/q/{token}", response_class=HTMLResponse)
def vendor_page(token: str):
    st, email = _by_token(token)
    v = view(st)
    rows = "".join(f"<tr><td>{l['n']}</td><td>{escape(l['description'])}</td><td>{escape(str(l['location']))}</td>"
                   f"<td class='n'>{escape(str(l['quantity']))}</td></tr>" for l in v["lines"])
    qs = "".join(f"<li>{escape(q)}</li>" for q in v["questionnaire"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(st['id'])} · Submit your quotation · {BRAND}</title><link rel="icon" href="data:image/svg+xml,{urllib.parse.quote(LOGO_SVG)}">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>body{{margin:0;background:#F4F7FB;color:#242526;font:500 14px/20px Geist,system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:32px 16px 64px}}.card{{background:#fff;border:1px solid #E3E6EE;border-radius:16px;padding:24px;margin-top:16px}}
h1{{font:700 24px/1.3 Geist,sans-serif;color:#100E5B;margin:4px 0}}h2{{font:600 16px Geist,sans-serif;color:#373192;margin:0 0 10px}}
.m{{color:#5B6170}}table{{width:100%;border-collapse:collapse}}th{{text-align:left;font-size:12px;color:#5B6170;border-bottom:1px solid #C9CEDA;padding:8px}}
td{{padding:8px;border-bottom:1px solid #E3E6EE;vertical-align:top}}.n{{text-align:right}}
label{{display:block;font-size:12px;margin:12px 0 6px}}input[type=text],input:not([type=file]){{width:100%;box-sizing:border-box;height:40px;border:1px solid #C9CEDA;border-radius:10px;padding:0 12px;font:500 14px Geist,system-ui,sans-serif}}
button{{margin-top:14px;height:40px;padding:0 18px;border:0;border-radius:10px;background:linear-gradient(90deg,#3B03C5,#9B38FC);color:#fff;font:500 14px Geist,sans-serif;cursor:pointer}}
button[disabled]{{opacity:.5}}#msg{{margin-top:10px}}.ok{{color:#114E0B}}.err{{color:#B42318}}ul{{margin:6px 0 0;padding-left:20px}}</style></head><body><main>
<div class="m" style="display:flex;align-items:center;gap:10px"><span style="width:28px;height:28px;display:inline-block">{LOGO_SVG}</span><b style="color:#242526;font-size:15px">{BRAND}</b></div><div class="m" style="margin-top:14px">Request for quotation · {escape(st['id'])}</div><h1>{escape(v['scope'] or '')}</h1>
<div class="card"><h2>Submit your quotation</h2>
<p class="m" style="margin-top:0">Upload your quote in any format: PDF, Excel, Word, CSV or a photo. One file per organisation: uploading again replaces your earlier file.</p>
<form id="f"><label for="org">Your organisation name *</label>
<input id="org" name="org" required maxlength="120" placeholder="e.g. Acme Traders Pvt Ltd" autocomplete="organization">
<label for="files">Quotation file *</label>
<input id="files" type="file" name="file" required accept=".pdf,.xlsx,.xls,.csv,.docx,.doc,.png,.jpg,.jpeg,.txt"> <button id="b">Upload</button></form>
<div id="msg"></div><ul id="mine"></ul></div>
<div class="card"><h2>Line items</h2><table><thead><tr><th>#</th><th>Item</th><th>Location</th><th class="n">Qty</th></tr></thead><tbody>{rows}</tbody></table></div>
{f'<div class="card"><h2>Please also answer</h2><ol>{qs}</ol></div>' if qs else ''}
<script>
const org=document.getElementById('org');try{{org.value=localStorage.getItem('org')||''}}catch(_){{}}
const f=document.getElementById('f'),b=document.getElementById('b'),msg=document.getElementById('msg'),mine=document.getElementById('mine');
f.addEventListener('submit',async e=>{{e.preventDefault();b.disabled=true;msg.className='m';msg.textContent='Uploading…';
 try{{const r=await fetch('/api/q/{token}/upload',{{method:'POST',body:new FormData(f)}});const d=await r.json();
  if(!r.ok)throw new Error(d.detail||r.statusText);
  msg.className='ok';msg.textContent='Received. Thank you.';try{{localStorage.setItem('org',org.value)}}catch(_){{}}document.getElementById('files').value='';
  mine.replaceChildren();const li=document.createElement('li');li.textContent=d.saved+(d.replaced.length?' (replaces '+d.replaced.join(', ')+')':'');mine.append(li);
 }}catch(x){{msg.className='err';msg.textContent=x.message;}}b.disabled=false;}});
</script></main></body></html>"""


def org_key(org: str) -> str:
    return " ".join((org or "").lower().split())


def store_upload(rid: str, org: str, email: Optional[str], source: str, filename: str, data: bytes) -> dict:
    """Save one proposal file. One file per organisation: an earlier file from the same organisation is superseded."""
    org = " ".join((org or "").split())[:120]
    if not org:
        raise HTTPException(422, "Please enter your organisation name")
    name = re.sub(r"[^\w.\- ]+", "_", Path(filename or "quote").name)[:120]
    if Path(name).suffix.lower() not in UPLOAD_EXT:
        raise HTTPException(422, f"{name}: this file type isn't accepted")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{name}: files can be up to 20 MB")
    if not data:
        raise HTTPException(422, f"{name} is empty")
    with _lock:
        st = load(rid)
        if st["status"] != "FINAL":
            raise HTTPException(409, "Finalise the RFQ first")
        n = max([u["n"] for u in st["uploads"]], default=0) + 1
        path = rdir(rid) / "uploads" / f"{n:03d}-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        replaced = []
        for u in st["uploads"]:
            if not u.get("superseded") and org_key(u.get("org") or u.get("email") or "") == org_key(org):
                u["superseded"] = n
                replaced.append(u["filename"])
        rec = {"n": n, "org": org, "email": email, "source": source, "filename": name, "size": len(data),
               "at": iso(config.real_now()), "path": path.name}
        st["uploads"].append(rec)
        save(st)
    return {**rec, "replaced": replaced}


@router.post("/api/q/{token}/upload")
async def vendor_upload(token: str, org: str = Form(...), file: UploadFile = File(...)):
    st, email = _by_token(token)
    rec = store_upload(st["id"], org, email, "email" if email else "link", file.filename, await file.read(MAX_UPLOAD + 1))
    return {"saved": rec["filename"], "replaced": rec["replaced"]}


@router.get("/api/live/rfqs/{rid}/uploads/{n}")
def get_upload(rid: str, n: int):
    st = load(rid)
    u = next((u for u in st["uploads"] if u["n"] == n), None)
    if not u:
        raise HTTPException(404, "No such file")
    return FileResponse(rdir(rid) / "uploads" / u["path"], filename=u["filename"])
