"""Analyst chat for a Live RFQ comparison run. Answers from the run's saved tables through fixed tools.

The model never does arithmetic and never runs code: every number comes from query_table or run_scenario, which work
only on the run's parquet tables. Tables it builds are saved to the run's derived/ folder, so later questions can reuse
them. Chat history is saved per RFQ in analysis/chat.json. Streams Server-Sent Events like Live RFQ:
chat (the transcript), tables (names that changed), open (open a proposal in the viewer), done.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config, live_compare as C, live_rfx as L, live_scorecard as SC, llm
from .util import iso

router = APIRouter()
_lock = threading.Lock()
MAX_ROWS = 60

SYSTEM = """You are 'Ana', a sharp procurement analyst. You help the buyer compare vendor proposals for one RFQ.
Keep answers short (2-6 sentences, or a small markdown table). Lead with the answer.

Rules:
- Every number you state must come from a tool result in this conversation. Never compute or estimate figures yourself;
  use query_table (filters, group_by, aggregations, sort) or run_scenario instead.
- Money is INR. Write it with Indian grouping or in crore/lakh (e.g. ₹10.24 crore).
- Name the table you used, e.g. "(vendor_summary)".
- Comparative questions about something the tables don't hold ("which vendor has the most detailed delivery plan", "who has
  the best support terms", "who is strongest on warranty"): call compare_vendors FIRST. It returns every vendor's evidence in
  one result. Judge only from it. Never name a winner after looking at some vendors, and never say what "the other proposals"
  contain unless this tool covered them. State your coverage ("I compared all 8 proposals" or which ones had thin evidence;
  scanned or photo proposals have no text excerpt, only the extracted answers). Give a ranking of the top vendors with the
  specific details that justify it, not only the winner. For a scored, repeatable answer offer a scorecard criterion.
- get_evidence is for one named vendor. When asked what else to compare on, call compare_vendors on a topic or two, then
  suggest 3-5 concrete parameters, and offer add_parameter to extract one from every proposal.
- Use save_as when you build a table the buyer may want again; say its name.
- When the buyer asks to see a proposal, call open_proposal.
- When asked why a vendor failed or what it missed, name the specific items: use the Fail Reasons, Missing Items and
  Items Not in RFQ columns of vendor_summary. Give both what it did not quote and what it quoted that the RFQ never asked
  for (extra items are a compliance miss), in the first answer, without waiting to be asked again.
- Scorecard: vendors are ranked by a weighted score out of 100 over criteria (price, lead time, one per RFQ question and any
  custom ones). The standard weights were proposed from the RFQ. When the buyer states weights or relations ("delivery twice
  as important as warranty") call update_weights with exactly what they said; the code does the arithmetic. Report the final
  weights and the new top vendors from the tool result. Weight and minimum changes recompute instantly and re-read nothing:
  say so. To add or change what a criterion rewards, call propose_criterion with a clear rubric (what earns 10, what earns 0)
  and pick a sensible weight if the buyer gave none. If it returns needs_approval, put its say_to_buyer question to the
  buyer and STOP: never call apply_pending until the buyer has answered yes in a later message (they may also press the
  Proceed button). Minimum scores that fail vendors are set with set_minimum.
- Compliance rules: FAIL on quantity shortfall, quoting items not in the RFQ, no questionnaire answers, a score below a criterion's minimum, or individual answers averaging below the QA
  threshold. A single blanket answer passes with a flag. Cherry-pick uses PASS vendors without bundle conditions, at RFQ
  quantities, before discounts.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_tables", "description": "List the run's tables with row counts and columns.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "describe_table", "description": "Columns, types, sample rows and numeric ranges of one table.",
        "parameters": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}}},
    {"type": "function", "function": {
        "name": "query_table",
        "description": "Filter, group, aggregate and sort one table. Returns up to 60 rows. Optionally saves the result as a derived table.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string"},
            "filters": {"type": "array", "items": {"type": "object", "properties": {
                "column": {"type": "string"},
                "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "not_in", "isnull", "notnull"]},
                "value": {"description": "A string, number, boolean, or a list for in/not_in. Omit for isnull/notnull."}},
                "required": ["column", "op"]}},
            "group_by": {"type": "array", "items": {"type": "string"}},
            "aggregations": {"type": "array", "items": {"type": "object", "properties": {
                "column": {"type": "string"}, "func": {"type": "string", "enum": ["sum", "mean", "min", "max", "count", "nunique"]},
                "as": {"type": "string", "description": "Name of the result column"}}, "required": ["column", "func"]}},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "Columns to return (when not aggregating)"},
            "sort": {"type": "array", "items": {"type": "object", "properties": {
                "column": {"type": "string"}, "descending": {"type": "boolean"}}, "required": ["column"]}},
            "limit": {"type": "integer"},
            "save_as": {"type": "string", "description": "snake_case name to save the result as derived/<name>"}},
            "required": ["table"]}}},
    {"type": "function", "function": {
        "name": "run_scenario",
        "description": "Recompute compliance, single-source ranking and cherry-pick with changed settings. Saves derived/<name>_summary and derived/<name>_cherry_pick.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "snake_case scenario name"},
            "exclude_vendors": {"type": "array", "items": {"type": "string"}},
            "fx_usd_inr": {"type": "number"}, "qa_threshold": {"type": "number"},
            "scorecard_version": {"type": "integer", "description": "Rank with an earlier scorecard version"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "compare_vendors",
        "description": "Evidence on a topic from EVERY vendor in one result: their extracted answers to the relevant RFQ questions, notes and terms, plus text excerpts around the keywords from the original documents (image-only proposals have none). Use it before comparing or ranking vendors on anything the tables don't hold. No model call; instant.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "What is being compared, e.g. 'delivery plan'"},
            "question_numbers": {"type": "array", "items": {"type": "integer"}, "description": "RFQ question numbers that relate to the topic (omit for all answers)"},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "Words to find in the documents, e.g. ['dispatch','tracking','warehouse','delivery']"}},
            "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "get_evidence",
        "description": "A vendor's full extraction (terms, notes, answers, sample rows) and, for text files, an excerpt of the proposal itself.",
        "parameters": {"type": "object", "properties": {"vendor": {"type": "string"}}, "required": ["vendor"]}}},
    {"type": "function", "function": {
        "name": "add_parameter",
        "description": "Read a new comparison parameter from every proposal (value, quote, confidence) and add it to the 'parameters' table and the Custom tab. Takes ~10-30s.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short title, e.g. 'Laptop warranty'"},
            "description": {"type": "string", "description": "What exactly to extract, e.g. 'Warranty period offered on laptops, in years'"}},
            "required": ["name", "description"]}}},
    {"type": "function", "function": {
        "name": "get_scorecard", "description": "The current scorecard: version, who set it, each criterion's id, name, weight (sums to 100), minimum score, rubric and reason, and any pending change.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "update_weights",
        "description": "Change criterion weights. Give explicit numbers in `weights` ({criterion id or name: number}) and/or `relations` ([{a, b, factor}] meaning weight of a = factor x weight of b). The code normalises to 100 and rejects relations that cannot all hold. Recomputes the rankings instantly; re-reads nothing.",
        "parameters": {"type": "object", "properties": {
            "weights": {"type": "object", "additionalProperties": {"type": "number"}},
            "relations": {"type": "array", "items": {"type": "object", "properties": {
                "a": {"type": "string"}, "b": {"type": "string"}, "factor": {"type": "number"}}, "required": ["a", "b", "factor"]}},
            "note": {"type": "string", "description": "Why the buyer asked, in a few words"}}}}},
    {"type": "function", "function": {
        "name": "set_minimum",
        "description": "Set or clear the minimum score (0-10) a vendor needs on a criterion; below it the vendor fails compliance. min_score null clears it. Recomputes instantly.",
        "parameters": {"type": "object", "properties": {"criterion": {"type": "string"}, "min_score": {"type": ["number", "null"]}},
                       "required": ["criterion"]}}},
    {"type": "function", "function": {
        "name": "propose_criterion",
        "description": "Add a custom criterion, or change the rubric of an existing one (give question_number to rescore an RFQ question under a new rubric). A new or changed rubric means re-reading every proposal, so this returns needs_approval with the cost: ask the buyer and wait. An unchanged rubric just updates weight/minimum.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "rubric": {"type": "string", "description": "What earns a 10, what earns a 0, e.g. '10 if it names the dispatch origin and a date for every site; 5 if only dates; 0 if neither'"},
            "weight": {"type": "number", "description": "Share of 100. If the buyer stated it, set weight_is_exact true; otherwise choose a sensible value (it is then scaled in with the others)"},
            "weight_is_exact": {"type": "boolean", "description": "true only if the buyer stated this weight"},
            "min_score": {"type": ["number", "null"]}, "question_number": {"type": ["integer", "null"]},
            "reason": {"type": "string", "description": "One sentence on why this weight"}},
            "required": ["name", "rubric"]}}},
    {"type": "function", "function": {
        "name": "apply_pending",
        "description": "Carry out a pending change (re-reads the proposals and scores them). Only after the buyer has said yes in a message sent AFTER you asked. Refused otherwise.",
        "parameters": {"type": "object", "properties": {"pending_id": {"type": "string"}}, "required": ["pending_id"]}}},
    {"type": "function", "function": {
        "name": "remove_criterion", "description": "Remove a criterion; its weight is redistributed. Recomputes instantly.",
        "parameters": {"type": "object", "properties": {"criterion": {"type": "string"}}, "required": ["criterion"]}}},
    {"type": "function", "function": {
        "name": "open_proposal", "description": "Open a vendor's proposal file in the viewer for the buyer.",
        "parameters": {"type": "object", "properties": {"vendor": {"type": "string"}, "page": {"type": "integer"}},
                       "required": ["vendor"]}}},
]


# ---- state --------------------------------------------------------------------------------------

def _file(rid: str):
    return C.adir(rid) / "chat.json"


def load_chat(rid: str) -> dict:
    f = _file(rid)
    return json.loads(f.read_text()) if f.exists() else {"messages": [], "chat": []}


def save_chat(rid: str, st: dict) -> None:
    C.adir(rid).mkdir(parents=True, exist_ok=True)
    _file(rid).write_text(json.dumps(st, ensure_ascii=False, indent=1))


def _context(rid: str, run_id: str) -> str:
    m = C.read_manifest(rid, run_id)
    v = L.view(L.load(rid))
    tabs = []
    for name in C.table_names(rid, run_id):
        df = C.load_table(rid, run_id, name)
        tabs.append(f"- {name} ({len(df)} rows): {', '.join(map(str, df.columns))}")
    vendors = ", ".join(p["org"] for p in m["proposals"])
    sc = SC.load_version(rid)
    scorecard = (f"Scorecard v{sc['version']} (set by {sc['author']}): {SC.describe(sc)}" if sc else "No scorecard yet (it is created by Run comparison)")
    pend = [p["title"] + f" [{p['id']}]" for p in SC.load_pending(rid) if p["status"] == "pending"]
    return (f"{SYSTEM}\n{scorecard}" + (f"\nPending (awaiting the buyer's yes): {'; '.join(pend)}" if pend else "") +
            f"\nRFQ {rid}: {v['scope']}\n{len(v['lines'])} lines, {len(v['questionnaire'])} questions. "
            f"Run {run_id} ({m['created_at']}), settings {json.dumps(m['settings'])}.\nVendors: {vendors}\n"
            f"Headline numbers: {json.dumps(m.get('summary', {}))}\n\nTables:\n" + "\n".join(tabs))


# ---- tools --------------------------------------------------------------------------------------

def _vendor(rid: str, run_id: str, name: str) -> dict:
    ps = C.read_manifest(rid, run_id)["proposals"]
    k = L.org_key(name)
    p = next((p for p in ps if L.org_key(p["org"]) == k), None) or next((p for p in ps if k and k in L.org_key(p["org"])), None)
    if not p:
        raise ValueError(f"No vendor named '{name}'. Vendors: {', '.join(p['org'] for p in ps)}")
    return p


def _col(df: pd.DataFrame, c: str) -> str:
    if c in df.columns:
        return c
    m = next((x for x in df.columns if str(x).lower() == str(c).lower()), None)
    if m is None:
        raise ValueError(f"No column '{c}'. Columns: {', '.join(map(str, df.columns))}")
    return m


def query(df: pd.DataFrame, a: dict) -> pd.DataFrame:
    for f in a.get("filters") or []:
        c, op, val = _col(df, f["column"]), f["op"], f.get("value")
        s = df[c]
        if op in ("in", "not_in"):
            vals = val if isinstance(val, list) else [val]
            mask = s.isin(vals) | s.astype(str).str.lower().isin([str(x).lower() for x in vals])
            mask = mask if op == "in" else ~mask
        elif op == "contains":
            mask = s.astype(str).str.contains(str(val), case=False, na=False, regex=False)
        elif op == "isnull":
            mask = s.isna()
        elif op == "notnull":
            mask = s.notna()
        elif op in ("eq", "ne") and isinstance(val, str) and (pd.api.types.is_string_dtype(s) or s.dtype == object):
            mask = s.astype(str).str.lower() == val.lower()
            mask = mask if op == "eq" else ~mask
        else:
            x = pd.to_numeric(s, errors="coerce") if isinstance(val, (int, float)) and not isinstance(val, bool) else s
            mask = {"eq": x == val, "ne": x != val, "gt": x > val, "gte": x >= val, "lt": x < val, "lte": x <= val}[op]
        df = df[mask.fillna(False)]
    gb = [_col(df, c) for c in a.get("group_by") or []]
    aggs = a.get("aggregations") or []
    if aggs:
        named = {(g.get("as") or f"{g['func']} of {g['column']}"): (_col(df, g["column"]), g["func"]) for g in aggs}
        if gb:
            df = df.groupby(gb, dropna=False).agg(**named).reset_index()
        else:
            df = pd.DataFrame([{k: getattr(df[c], f)() for k, (c, f) in named.items()}])
    elif gb:
        df = df.groupby(gb, dropna=False).size().reset_index(name="count")
    elif a.get("columns"):
        df = df[[_col(df, c) for c in a["columns"]]]
    for srt in reversed(a.get("sort") or []):
        df = df.sort_values(_col(df, srt["column"]), ascending=not srt.get("descending", False), kind="stable")
    if a.get("limit"):
        df = df.head(int(a["limit"]))
    return df


def _out(df: pd.DataFrame) -> str:
    return json.dumps({"rows": len(df), "data": C.records(df, MAX_ROWS),
                       "note": f"showing first {MAX_ROWS}" if len(df) > MAX_ROWS else None}, ensure_ascii=False, default=str)


def _sc_result(rid: str, v: dict, extra: Optional[dict] = None) -> str:
    ranking = SC.rebuild(rid)
    return json.dumps({"scorecard_version": v["version"], "weights": [{"id": c["id"], "name": c["name"], "weight": c["weight"], "min_score": c.get("min_score")}
                                                                      for c in v["criteria"]], "ranking": ranking, **(extra or {})}, default=str)


def _windows(text: str, keywords: list[str], width: int = 260, limit: int = 4) -> list[str]:
    """Short stretches of the document around the first matches of the keywords, so one result can cover every vendor."""
    low, out, last = text.lower(), [], -10**9
    hits = sorted(i for k in keywords if k for i in [low.find(k.lower())] if i >= 0)
    for i in hits:
        if i - last < width:
            continue
        out.append(" ".join(text[max(0, i - width // 2): i + width].split()))
        last = i
        if len(out) >= limit:
            break
    return out


def compare_vendors(rid: str, run_id: str, topic: str, question_numbers: list[int], keywords: list[str]) -> str:
    """Every vendor's evidence on a topic in one result, so a ranking can't be made from a sample."""
    st = L.load(rid)
    m = C.read_manifest(rid, run_id)
    by_pid = {e["pid"]: e for e in C.extractions(rid, run_id)}
    keywords = keywords or [w for w in topic.lower().split() if len(w) > 3]
    vendors, image_only, failed = [], [], []
    for p in m["proposals"]:
        ex = by_pid.get(p["pid"])
        if not ex:
            failed.append(p["org"])
            continue
        prop = ex["proposal"]
        answers = [{"question_number": q.get("question_number"), "question": q["question"][:140], "answer": q["answer"], "score": q["confidence_score"]}
                   for q in prop["questionnaire_answers"] if not question_numbers or q.get("question_number") in question_numbers or q.get("question_number") is None]
        try:
            parts = C.read_parts(C._upload_path(rid, st, p["n"]))
            text = "\n".join(x.get("text", "") for x in parts if x.get("type") == "input_text")
        except Exception:
            text = ""
        windows = _windows(text, keywords) if text else []
        if not text:
            image_only.append(p["org"])
        vendors.append({"vendor": p["org"], "questionnaire_style": prop["questionnaire_style"], "answers": answers,
                        "lead_time_days": prop.get("lead_time_days"), "payment_terms": prop.get("payment_terms"), "freight_included": prop.get("freight_included"),
                        "special_notes": prop.get("special_notes"), "document_excerpts": windows or ("(no text in this document: image or scan)" if not text else "(no match for the keywords)")})
    return json.dumps({"topic": topic, "coverage": {"vendors_in_run": len(m["proposals"]), "vendors_returned": len(vendors), "image_only_no_excerpts": image_only, "not_extracted": failed},
                       "instruction": "Judge only from this result. Name every vendor you compare and state this coverage in your answer.", "vendors": vendors},
                      ensure_ascii=False, default=str)


def call_tool(rid: str, run_id: str, name: str, a: dict, events: list, ctx: Optional[dict] = None) -> str:
    ctx = ctx or {}
    if name == "get_scorecard":
        sc, _ = SC.ensure_standard(rid)
        return json.dumps({**SC.overview(rid), "criteria": [{k: c.get(k) for k in ("id", "name", "weight", "min_score", "rubric", "reason")} for c in sc["criteria"]]}, default=str)
    if name in ("update_weights", "set_minimum", "remove_criterion"):
        if name == "update_weights":
            v = SC.apply_weights(rid, a.get("weights"), a.get("relations"), a.get("note") or "")
        elif name == "set_minimum":
            sc, _ = SC.ensure_standard(rid)
            cid = SC.find(sc, a["criterion"])["id"]
            ms = a.get("min_score")
            v = SC.update(rid, lambda cs: [{**c, "min_score": (None if ms is None else float(ms))} if c["id"] == cid else c for c in cs],
                          f"Minimum on {cid}: {'cleared' if ms is None else ms}")
        else:
            v = SC.remove_criterion(rid, a["criterion"])
        events.append(("tables", ["vendor_summary", "scorecard"]))
        return _sc_result(rid, v, {"note": "Recomputed from saved tables; nothing was re-read."})
    if name == "propose_criterion":
        res = SC.propose_criterion(rid, a["name"], a.get("rubric", ""), a.get("weight"), a.get("min_score"), a.get("question_number"),
                                   ctx.get("user_msgs", 0), a.get("reason", ""), bool(a.get("weight_is_exact")))
        if res.get("applied"):
            events.append(("tables", ["vendor_summary", "scorecard"]))
            return _sc_result(rid, SC.load_version(rid), {"note": "Rubric unchanged: weight or minimum updated, nothing re-read."})
        events.append(("pending", [res["pending_id"]]))
        return json.dumps(res)
    if name == "apply_pending":
        pend = next((p for p in SC.load_pending(rid) if p["id"] == a.get("pending_id")), None)
        if not pend:
            return "Error: no such pending change"
        if not SC.typed_approval_ok(pend, ctx.get("user_text", ""), ctx.get("user_msgs", 0)):
            return ("Refused: the buyer has not said yes since you proposed this. Ask them (say_to_buyer) and wait; "
                    "they can also press the Proceed button.")
        res = SC.execute_pending(rid, pend["id"])
        events.append(("tables", ["vendor_summary", "scorecard"]))
        return json.dumps({"scorecard_version": res["version"], "ranking": res["ranking"], "note": "Proposals were re-read and scored."}, default=str)
    if name == "list_tables":
        return json.dumps([{"table": n, "rows": len(df := C.load_table(rid, run_id, n)), "columns": list(map(str, df.columns))}
                           for n in C.table_names(rid, run_id)])
    if name == "describe_table":
        df = C.load_table(rid, run_id, a["table"])
        num = df.select_dtypes("number")
        return json.dumps({"rows": len(df), "dtypes": {str(k): str(v) for k, v in df.dtypes.items()}, "sample": C.records(df, 5),
                           "ranges": json.loads(num.agg(["min", "max"]).to_json()) if not num.empty else {}}, default=str)
    if name == "query_table":
        df = query(C.load_table(rid, run_id, a["table"]), a)
        res = json.loads(_out(df))
        if a.get("save_as"):
            res["saved_as"] = C.save_derived(rid, run_id, a["save_as"], df)
            events.append(("tables", [res["saved_as"]]))
        return json.dumps(res, ensure_ascii=False, default=str)
    if name == "run_scenario":
        m = C.read_manifest(rid, run_id)
        settings = {**m["settings"], **{k: a[k] for k in ("fx_usd_inr", "qa_threshold") if a.get(k) is not None}}
        tables, summary = C.compute(rid, run_id, settings, exclude=a.get("exclude_vendors"), scorecard_version=a.get("scorecard_version"))
        base = a.get("name") or "scenario"
        saved = [C.save_derived(rid, run_id, f"{base}_summary", tables["vendor_summary"]),
                 C.save_derived(rid, run_id, f"{base}_cherry_pick", tables["cherry_pick"])]
        events.append(("tables", saved))
        vs = tables["vendor_summary"]
        top = vs[[c for c in ("Rank", "Vendor", "Compliance Status", "Score / 100", "Fail Reasons", "Final Landed Cost (INR)") if c in vs.columns]]
        return json.dumps({"summary": summary, "ranking": C.records(top), "saved": saved}, default=str)
    if name == "compare_vendors":
        return compare_vendors(rid, run_id, a.get("topic", ""), a.get("question_numbers") or [], a.get("keywords") or [])
    if name == "get_evidence":
        p = _vendor(rid, run_id, a["vendor"])
        ex = next((e for e in C.extractions(rid, run_id) if e["pid"] == p["pid"]), None)
        if not ex:
            return json.dumps({"vendor": p["org"], "error": "This proposal wasn't extracted in this run"})
        prop = dict(ex["proposal"])
        items = prop.pop("line_items")
        prop["line_items_sample"], prop["line_items_total"] = items[:15], len(items)
        excerpt = None
        try:
            part = C.read_parts(C._upload_path(rid, L.load(rid), p["n"]))[0]
            excerpt = part.get("text", "")[:8000] or "(image proposal: no text; rely on the extraction)"
        except Exception as e:
            excerpt = f"(couldn't read file: {e})"
        return json.dumps({"vendor": p["org"], "file": p["filename"], "extraction": prop, "source_excerpt": excerpt}, ensure_ascii=False, default=str)
    if name == "add_parameter":
        df = C.extract_parameter(rid, run_id, a["name"], a["description"])
        events.append(("tables", ["parameters"]))
        return _out(df[["Vendor", "Value", "Evidence", "Confidence"]])
    if name == "open_proposal":
        p = _vendor(rid, run_id, a["vendor"])
        events.append(("open", {"n": p["n"], "page": a.get("page")}))
        return f"Opened {p['org']} ({p['filename']}) for the buyer."
    return f"Unknown tool {name}"


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ---- routes -------------------------------------------------------------------------------------

class AskIn(BaseModel):
    text: str
    run_id: Optional[str] = None


@router.get("/api/live/rfqs/{rid}/analyst")
def get_chat(rid: str):
    L.load(rid)
    return {"chat": load_chat(rid)["chat"], "pending": [p for p in SC.load_pending(rid) if p["status"] == "pending"]}


@router.post("/api/live/rfqs/{rid}/analyst/reset")
def reset_chat(rid: str):
    save_chat(rid, {"messages": [], "chat": []})
    return {"chat": []}


@router.post("/api/live/rfqs/{rid}/analyst/messages")
def ask(rid: str, body: AskIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Write a question")
    run_id = body.run_id or C.latest(rid)
    if not run_id:
        raise HTTPException(409, "Run a comparison first")
    if C.read_manifest(rid, run_id)["status"] == "running":
        raise HTTPException(409, "The comparison is still running")

    def stream():
        with _lock:
            st = load_chat(rid)
            msgs = [{"role": "system", "content": _context(rid, run_id)}] + st["messages"][1:]
            msgs.append({"role": "user", "content": text})
            st["chat"].append({"role": "user", "text": text, "run_id": run_id})
            yield _sse("chat", st["chat"])
            try:
                for _ in range(12):                       # tool rounds per question
                    resp = llm.client().chat.completions.create(model=config.MODEL, messages=msgs, tools=TOOLS,
                                                                reasoning_effort="none")
                    m = resp.choices[0].message
                    msgs.append(m.model_dump(exclude_none=True))
                    if m.content and m.tool_calls:
                        st["chat"].append({"role": "assistant", "text": m.content})
                        yield _sse("chat", st["chat"])
                    if not m.tool_calls:
                        st["chat"].append({"role": "assistant", "text": m.content or ""})
                        break
                    for tc in m.tool_calls:
                        yield _sse("tool", {"name": tc.function.name})
                        events: list = []
                        try:
                            out = call_tool(rid, run_id, tc.function.name, json.loads(tc.function.arguments or "{}"), events,
                                            {"user_text": text, "user_msgs": sum(1 for c in st["chat"] if c["role"] == "user")})
                        except Exception as e:            # bad column, unknown vendor: tell the model so it can retry
                            out = f"Error: {type(e).__name__}: {str(e)[:300]}"
                        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                        for ev, data in events:
                            yield _sse(ev, data)
            except Exception as e:
                st["chat"].append({"role": "assistant", "text": f"Sorry, I hit a problem: {str(e)[:200]}", "error": True})
            st["messages"] = msgs
            st["updated_at"] = iso(config.real_now())
            save_chat(rid, st)
            yield _sse("chat", st["chat"])
            yield _sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
