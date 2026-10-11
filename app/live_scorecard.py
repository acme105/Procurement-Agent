"""Weighted scorecard for a Live RFQ comparison.

A scorecard is a versioned list of criteria (price, lead time, one per RFQ question, plus custom ones the buyer adds in
the analyst chat). Each criterion gives every vendor a 0-10 score; the vendor's total is the weighted average out of 100.
A criterion may carry a minimum score that fails a vendor. The model proposes the starting weights from the RFQ; weights
or relations the buyer states in chat override them (that arithmetic is done here, never by the model).

Re-reading vendor documents happens only when a criterion's rubric is new or changed, and only after the buyer approves
(a button, or a typed "yes" to Ana's question). Weight and minimum changes recompute from saved tables, with no model calls.

Storage, per RFQ:  analysis/scorecard/v<N>.json, current.txt, pending.json, scores/<criterion>.json
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import queue
import re
import threading
from pathlib import Path
from typing import Callable, Literal, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config, live_compare as C, live_rfx as L, llm
from .util import hash_obj, iso

router = APIRouter()
STD_PROMPT = "live_scorecard.v2"
CRIT_PROMPT = "live_criterion.v1"
_lock = threading.RLock()
SCORE_COLS = ["Criterion", "Criterion Id", "Weight", "Min Score", "Vendor", "Score", "Points", "Raw", "Reason", "Quote", "Source"]
AFFIRM = re.compile(r"^\s*(yes|yep|yeah|yup|sure|ok|okay|proceed|go ahead|go on|do it|please do|approve|approved|confirm|confirmed|sounds good|go)\b", re.I)


# ---- schemas -----------------------------------------------------------------------------------

class WeightItem(BaseModel):
    id: str
    weight: float = Field(description="Importance for this RFQ, 0-100. The weights across all criteria should sum to about 100.")
    reason: str = Field(description="One short sentence on why this weight suits this RFQ.")


class WeightProposal(BaseModel):
    criteria: list[WeightItem]


class CriterionScore(BaseModel):
    score: float = Field(description="0 to 10 against the rubric. 10 = fully meets it, 0 = absent or contradicts it.")
    reason: str = Field(description="One sentence explaining the score.")
    quote: Optional[str] = Field(description="A short verbatim quote from the proposal supporting the score. null if nothing relevant.")
    confidence: Literal["High", "Medium", "Low"]


# ---- storage -----------------------------------------------------------------------------------

def sdir(rid: str) -> Path:
    return C.adir(rid) / "scorecard"


def current_version(rid: str) -> int:
    f = sdir(rid) / "current.txt"
    return int(f.read_text().strip()) if f.exists() else 0


def load_version(rid: str, n: Optional[int] = None) -> Optional[dict]:
    n = n if n is not None else current_version(rid)
    f = sdir(rid) / f"v{n}.json"
    return json.loads(f.read_text()) if n and f.exists() else None


def versions(rid: str) -> list[dict]:
    d = sdir(rid)
    out = []
    for f in sorted(d.glob("v*.json"), key=lambda p: int(p.stem[1:])) if d.exists() else []:
        v = json.loads(f.read_text())
        out.append({k: v.get(k) for k in ("version", "author", "note", "created_at")})
    return out


def normalise(criteria: list[dict]) -> list[dict]:
    total = sum(max(0.0, float(c["weight"])) for c in criteria)
    if not criteria or total <= 0:
        raise ValueError("Weights must add up to more than zero")
    return [{**c, "weight": round(max(0.0, float(c["weight"])) * 100 / total, 2)} for c in criteria]


def save_version(rid: str, criteria: list[dict], author: str, note: str) -> dict:
    with _lock:
        d = sdir(rid)
        d.mkdir(parents=True, exist_ok=True)
        n = current_version(rid) + 1
        v = {"version": n, "author": author, "note": note, "created_at": iso(config.real_now()), "criteria": normalise(criteria)}
        (d / f"v{n}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1))
        (d / "current.txt").write_text(str(n))
        return v


def rubric_hash(crit: dict) -> str:
    return hash_obj({"rubric": (crit.get("rubric") or "").strip(), "question": crit.get("question_number")}) if (crit.get("rubric") or "").strip() else ""


def candidates(rid: str) -> list[dict]:
    v = L.view(L.load(rid))
    out = [{"id": "price", "name": "Price (landed cost)", "kind": "price"},
           {"id": "lead_time", "name": "Lead time", "kind": "lead_time"}]
    for i, q in enumerate(v["questionnaire"], 1):
        out.append({"id": f"q{i}", "name": f"Q{i}: {C._short(q, 60)}", "kind": "question", "question_number": i, "question": q})
    return out


# ---- the standard scorecard (model proposes weights from the RFQ) -----------------------------------

def ensure_standard(rid: str) -> tuple[dict, bool]:
    """Current scorecard; creates version 1 from the RFQ if there is none. Returns (scorecard, created)."""
    with _lock:
        cur = load_version(rid)
        if cur:
            return cur, False
        cands = candidates(rid)
        v = L.view(L.load(rid))
        weights, note = None, "Starting scorecard proposed from the RFQ"
        try:
            ctx = C.rfq_context(v)
            listing = "\n".join(f"- {c['id']}: {c['name']}" for c in cands)
            instructions = ("You are a procurement analyst designing a vendor scorecard for one RFQ. Give each criterion an importance "
                            "weight (0-100, summing to about 100) that suits THIS RFQ, with one short reason each. Think in groups first: "
                            "price, delivery (lead time and the delivery question) and the remaining questionnaire items together. For "
                            "a commodity purchase price usually carries the most weight on its own (often 30-45), lead time 10-15, and "
                            "the questionnaire items share the rest, with contractual-risk questions (warranty, penalties, payment "
                            "terms, support) above routine confirmations. Many questions must not drown out price: weigh a single "
                            "question against price, not just against other questions. Return every criterion id exactly once and no "
                            "other ids.\n\nRFQ:\n" + ctx + "\n\nCriteria:\n" + listing)
            parsed, _ = llm.call("live_scorecard", STD_PROMPT, WeightProposal, instructions, [llm.text_part("Propose the weights.")],
                                 effort="low", max_output_tokens=4000)
            got = {w.id: w for w in parsed.criteria}
            if set(got) != {c["id"] for c in cands}:
                raise ValueError("model did not return exactly the candidate criteria")
            weights = {i: (max(0.0, w.weight), w.reason) for i, w in got.items()}
            if sum(w for w, _ in weights.values()) <= 0:
                raise ValueError("weights sum to zero")
        except Exception as e:           # model unavailable or invalid: equal weights, said plainly
            weights = {c["id"]: (1.0, "Equal weight (the model's proposal was unavailable)") for c in cands}
            note = f"Equal weights: starting proposal unavailable ({str(e)[:80]})"
        crits = [{**{k: v for k, v in c.items() if k != "question"}, "weight": weights[c["id"]][0], "reason": weights[c["id"]][1],
                  "min_score": None, "rubric": None} for c in cands]
        return save_version(rid, crits, "model", note), True


def describe(sc: dict) -> str:
    return ", ".join(f"{c['name'].split(':')[0] if c['kind'] == 'question' else c['name']} {c['weight']:g}" for c in sc["criteria"])


# ---- scoring a criterion for every vendor (formulas, answers, or re-reading the documents) -------------

def _scores_file(rid: str, crit: dict) -> Path:
    """Scores are kept per criterion AND rubric, so every scorecard version keeps its own numbers."""
    return sdir(rid) / "scores" / f"{crit['id']}__{(rubric_hash(crit) or 'none')[:12]}.json"


def load_custom(rid: str, crit: dict) -> dict:
    f = _scores_file(rid, crit)
    legacy = sdir(rid) / "scores" / f"{crit['id']}.json"          # saved before scores were keyed by rubric
    if not f.exists() and legacy.exists() and json.loads(legacy.read_text()).get("rubric_hash") == rubric_hash(crit):
        f = legacy
    data = json.loads(f.read_text()) if f.exists() else {}
    for item in data.get("items", {}).values():                 # results saved before the failed flag existed
        if str(item.get("reason", "")).startswith("Could not be scored"):
            item["failed"] = True
    return data


def apply(rid: str, vsum: pd.DataFrame, qdf: pd.DataFrame, version: Optional[int] = None) -> Optional[tuple[pd.DataFrame, pd.DataFrame]]:
    """Add scores, criterion gates and ranks to the vendor summary. None if the RFQ has no scorecard yet."""
    sc = load_version(rid, version)
    if not sc or vsum.empty:
        return None
    crits = sc["criteria"]
    complete = vsum[(vsum["Quantity Shortfall"] == 0)]
    min_cost = complete["Final Landed Cost (INR)"].min() if not complete.empty else None
    leads = pd.to_numeric(vsum["Lead Time (Days)"], errors="coerce")
    min_lead = leads[leads > 0].min() if (leads > 0).any() else None
    rows = []
    for _, v in vsum.iterrows():
        vendor = v["Vendor"]
        qa = qdf[qdf["Vendor"] == vendor]
        for c in crits:
            raw = score = reason = quote = None
            source = "formula"
            if (c.get("rubric") or "").strip():                       # custom rubric: scored by re-reading the document
                stored = load_custom(rid, c)
                hit = stored.get("items", {}).get(L.org_key(vendor)) if stored.get("rubric_hash") == rubric_hash(c) else None
                source = "document"
                if hit:
                    score, reason, quote, raw = float(hit["score"]), hit.get("reason"), hit.get("quote"), hit.get("confidence")
                    if hit.get("failed"):
                        source = "failed"
                else:
                    score, reason = 0.0, "Not scored yet (run the comparison or approve the pending change)"
            elif c["kind"] == "price":
                cost = v["Final Landed Cost (INR)"]
                if min_cost and v["Quantity Shortfall"] == 0 and cost:
                    score = 10 * min_cost / cost
                    reason = "Lowest landed cost scores 10; others in proportion"
                else:
                    score, reason = 0.0, "Incomplete quote: price not comparable"
                raw = cost
            elif c["kind"] == "lead_time":
                lead = pd.to_numeric(v["Lead Time (Days)"], errors="coerce")
                if pd.notna(lead) and lead > 0 and min_lead:
                    score, reason = 10 * min_lead / lead, "Shortest lead time scores 10; others in proportion"
                else:
                    score, reason = 0.0, "Lead time not stated"
                raw = None if pd.isna(lead) else float(lead)
            else:                                                       # question scored from the extraction's answer score
                n = c.get("question_number")
                source = "answers"
                hit = qa[qa["Question #"] == n]
                if len(hit):
                    score, reason, raw = float(hit.iloc[0]["Confidence Score"]), hit.iloc[0]["Confidence Reasoning"], hit.iloc[0]["Answer"]
                else:
                    blanket = qa[qa["Question #"].isna()]
                    if len(blanket):
                        score, reason, raw = float(blanket.iloc[0]["Confidence Score"]), "Blanket answer: " + str(blanket.iloc[0]["Confidence Reasoning"]), blanket.iloc[0]["Answer"]
                    else:
                        score, reason = 0.0, "No answer to this question"
            score = max(0.0, min(10.0, float(score)))
            rows.append({"Criterion": c["name"], "Criterion Id": c["id"], "Weight": c["weight"], "Min Score": c.get("min_score"),
                         "Vendor": vendor, "Score": round(score, 2), "Points": round(c["weight"] * score / 10, 2),
                         "Raw": None if raw is None else str(raw)[:300], "Reason": reason, "Quote": quote, "Source": source})
    tbl = pd.DataFrame(rows, columns=SCORE_COLS)
    vs = vsum.copy()
    totals = tbl.groupby("Vendor")["Points"].sum().round(1)
    vs["Score / 100"] = vs["Vendor"].map(totals)
    vs["Scorecard Version"] = sc["version"]
    for i, v in vs.iterrows():                                          # criterion minimums fail a vendor
        gate = []
        for c in crits:
            row = tbl[(tbl["Vendor"] == v["Vendor"]) & (tbl["Criterion Id"] == c["id"])].iloc[0]
            if row["Source"] == "failed":                  # a technical failure is not the vendor's fault: flag it, don't fail the vendor
                vs.at[i, "Flags"] = "; ".join(filter(None, [v["Flags"] if isinstance(v["Flags"], str) else "", f"{c['name'].split(':')[0]} could not be scored (retry)"]))
                continue
            if c.get("min_score") is None:
                continue
            s = float(row["Score"])
            if s < float(c["min_score"]):
                gate.append(f"Below minimum on {c['name'].split(':')[0] if c['kind'] == 'question' else c['name']} ({s:.1f} < {float(c['min_score']):g})")
        if gate:
            vs.at[i, "Fail Reasons"] = "; ".join(([v["Fail Reasons"]] if isinstance(v["Fail Reasons"], str) and v["Fail Reasons"] else []) + gate)
            vs.at[i, "Compliance Status"] = "FAIL"
    cost_order = vs.sort_values(["Compliance Status", "Final Landed Cost (INR)"], ascending=[False, True]).index
    vs["Cost rank"] = pd.Series(range(1, len(vs) + 1), index=cost_order)
    vs = vs.sort_values(["Compliance Status", "Score / 100", "Final Landed Cost (INR)"], ascending=[False, False, True]).reset_index(drop=True)
    vs.insert(0, "Rank", range(1, len(vs) + 1))
    return vs, tbl


# ---- changing the scorecard --------------------------------------------------------------------------

def find(sc: dict, ref: str) -> dict:
    """A criterion by id or (partial, case-insensitive) name."""
    r = (ref or "").strip().lower()
    for key in ("id", "name"):
        hit = [c for c in sc["criteria"] if str(c[key]).lower() == r]
        if len(hit) == 1:
            return hit[0]
    hit = [c for c in sc["criteria"] if r and (r in c["name"].lower() or r in c["id"].lower())]
    if len(hit) == 1:
        return hit[0]
    names = ", ".join(f"{c['id']} ({c['name']})" for c in sc["criteria"])
    raise ValueError(f"{'Ambiguous' if hit else 'No'} criterion '{ref}'. Criteria: {names}")


def new_weights(sc: dict, weights: Optional[dict], relations: Optional[list]) -> dict[str, float]:
    """Final weights (summing to 100) from the current ones, explicit numbers and 'a = factor x b' relations.

    Explicit numbers are final shares of 100: "price 70" gives price exactly 70. A relation to an explicit criterion fixes
    the other one as well ("delivery 30, warranty half of delivery" gives warranty 15). The criteria that remain share what
    is left in proportion to their current weights, with relations among themselves applied first. If the relations and
    numbers cannot all hold, this raises instead of guessing."""
    ids = [c["id"] for c in sc["criteria"]]
    current = {c["id"]: float(c["weight"]) for c in sc["criteria"]}
    fixed: dict[str, float] = {}
    for ref, val in (weights or {}).items():
        if float(val) < 0:
            raise ValueError("Weights can't be negative")
        fixed[find(sc, ref)["id"]] = float(val)
    rels = []
    for r in relations or []:
        a, b, f = find(sc, r["a"])["id"], find(sc, r["b"])["id"], float(r["factor"])
        if a == b or f <= 0:
            raise ValueError("A relation needs two different criteria and a positive factor")
        rels.append((a, b, f))
    changed = True
    while changed:                                  # a relation to a fixed criterion fixes the other one too
        changed = False
        for a, b, f in rels:
            if b in fixed and a not in fixed:
                fixed[a], changed = f * fixed[b], True
            elif a in fixed and b not in fixed:
                fixed[b], changed = fixed[a] / f, True
    if sum(fixed.values()) > 100.01:
        raise ValueError(f"The weights given add up to {sum(fixed.values()):.0f}, more than 100. Ask the buyer which to lower.")
    rest = [i for i in ids if i not in fixed]
    raw = {i: current[i] for i in rest}
    for _ in range(max(1, len(rels))):              # relations among the remaining criteria
        for a, b, f in rels:
            if a in raw and b in raw:
                raw[a] = f * raw[b]
    final = dict(fixed)
    if rest:
        room, total = 100 - sum(fixed.values()), sum(raw.values())
        for i in rest:
            final[i] = room * (raw[i] / total if total > 0 else 1 / len(rest))
    else:
        scale = 100 / sum(final.values()) if sum(final.values()) > 0 else 0
        if scale == 0:
            raise ValueError("Weights must add up to more than zero")
        final = {i: v * scale for i, v in final.items()}
    for a, b, f in rels:
        if final[b] > 0 and abs(final[a] / final[b] - f) > 0.01 * f:
            raise ValueError(f"These can't all hold: {find(sc, a)['name']} = {f:g} x {find(sc, b)['name']} conflicts with the "
                             f"weights or relations given. Ask the buyer which to keep.")
    return {i: final[i] for i in ids}


def update(rid: str, mutate: Callable[[list[dict]], list[dict]], note: str, author: str = "buyer") -> dict:
    sc, _ = ensure_standard(rid)
    crits = mutate([dict(c) for c in sc["criteria"]])
    return save_version(rid, crits, author, note)


def apply_weights(rid: str, weights: Optional[dict], relations: Optional[list], note: str = "") -> dict:
    sc, _ = ensure_standard(rid)
    final = new_weights(sc, weights, relations)
    desc = "; ".join(filter(None, [note, "weights: " + ", ".join(f"{find(sc, k)['id']}={v:.0f}" for k, v in (weights or {}).items()),
                                   "relations: " + ", ".join(f"{r['a']}={r['factor']}x{r['b']}" for r in (relations or []))]))[:240]
    return save_version(rid, [{**c, "weight": final[c["id"]]} for c in sc["criteria"]], "buyer", desc or "Weights changed")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "criterion"


# ---- pending changes: a new or changed rubric needs re-reading the documents, so the buyer must approve ----------

def _pending_file(rid: str) -> Path:
    return sdir(rid) / "pending.json"


def load_pending(rid: str) -> list[dict]:
    f = _pending_file(rid)
    return json.loads(f.read_text()) if f.exists() else []


def save_pending(rid: str, items: list[dict]) -> None:
    sdir(rid).mkdir(parents=True, exist_ok=True)
    _pending_file(rid).write_text(json.dumps(items, ensure_ascii=False, indent=1))


def estimate_seconds(rid: str, props: list[dict]) -> int:
    """Rough time to re-read: scans and photos are slow (minutes), text documents quick; four are read at once."""
    st = L.load(rid)
    each = []
    for p in props:
        u = next((u for u in st["uploads"] if u["n"] == p["n"]), None)
        ext = Path(u["filename"]).suffix.lower() if u else ""
        each.append(150 if ext in (".png", ".jpg", ".jpeg") else 40 if ext == ".pdf" else 15)
    return int(math.ceil(max(max(each, default=0), sum(each) / 4) / 10) * 10) if each else 10


def needs_scoring(rid: str, crit: dict, props: list[dict]) -> list[dict]:
    """Proposals with no good score for this rubric: all of them if the rubric is new or changed, else the missing or failed ones."""
    stored = load_custom(rid, crit)
    if stored.get("rubric_hash") != rubric_hash(crit):
        return props
    items = stored.get("items", {})
    return [p for p in props if L.org_key(p["org"]) not in items or items[L.org_key(p["org"])].get("failed")]


def _proposals_to_score(rid: str, run_id: Optional[str] = None) -> list[dict]:
    run = run_id or C.latest(rid)
    if not run:
        return []
    return [p for p in C.read_manifest(rid, run)["proposals"] if p["status"] == "done"]


def place_criterion(crits: list[dict], spec: dict) -> list[dict]:
    """Insert or replace a criterion. A weight the buyer stated is an exact share of 100 (the others scale to fit);
    a weight the model chose is relative and is normalised with the rest."""
    ids = {c["id"] for c in crits}
    out = [spec if c["id"] == spec["id"] else c for c in crits] if spec["id"] in ids else crits + [spec]
    if spec.get("exact"):
        final = new_weights({"criteria": out}, {spec["id"]: spec["weight"]}, None)
        out = [{**c, "weight": final[c["id"]]} for c in out]
    return [{k: v for k, v in c.items() if k != "exact"} for c in out]


def propose_criterion(rid: str, name: str, rubric: str, weight: Optional[float], min_score: Optional[float],
                      question_number: Optional[int], user_msgs: int, reason: str = "", exact: bool = False) -> dict:
    """Add or change a criterion. Re-reading is needed only if the rubric is new or changed: then this stores a pending
    change and returns what it would cost; otherwise it applies at once."""
    sc, _ = ensure_standard(rid)
    rubric = (rubric or "").strip()
    if not rubric:
        raise ValueError("A criterion needs a rubric: what earns a 10 and what earns a 0")
    cid = f"q{int(question_number)}" if question_number else slug(name)
    existing = next((c for c in sc["criteria"] if c["id"] == cid), None)
    spec = {"id": cid, "name": (existing or {}).get("name") or name, "kind": "question" if question_number else "custom",
            "question_number": int(question_number) if question_number else None, "rubric": rubric,
            "weight": float(weight) if weight is not None else (existing["weight"] if existing else round(100 / (len(sc["criteria"]) + 1), 1)),
            "min_score": float(min_score) if min_score is not None else (existing or {}).get("min_score"),
            "reason": reason or (existing or {}).get("reason") or "Added by the buyer",
            "exact": bool(exact and weight is not None)}
    props = _proposals_to_score(rid)
    todo = needs_scoring(rid, spec, props) if existing and rubric_hash(existing) == rubric_hash(spec) else props
    if existing and rubric_hash(existing) == rubric_hash(spec) and not todo:
        v = save_version(rid, place_criterion(sc["criteria"], spec), "buyer", f"{spec['name']}: weight or minimum changed")
        return {"applied": True, "version": v["version"], "reread": False}
    with _lock:
        items = [p for p in load_pending(rid) if not (p["status"] == "pending" and p["criterion"]["id"] == cid)]
        est = estimate_seconds(rid, todo)
        pend = {"id": f"c{len(items) + 1}-{cid}"[:40], "status": "pending", "criterion": spec, "created_at": iso(config.real_now()),
                "proposals": len(todo), "estimate_s": est, "user_msgs": user_msgs,
                "title": f"Re-read {len(todo)} proposals to score “{spec['name']}”"}
        items.append(pend)
        save_pending(rid, items)
    mins = f"{pend['estimate_s']} s" if pend["estimate_s"] < 90 else f"about {round(pend['estimate_s'] / 60)} min"
    return {"applied": False, "needs_approval": True, "pending_id": pend["id"], "proposals": len(todo), "estimate_seconds": pend["estimate_s"],
            "criterion": spec["name"], "scanned_or_photo_proposals_are_slow": True,
            "say_to_buyer": f"This re-reads {len(todo)} proposals (about {mins}; scans and photos take longest). Proceed?"}


def score_criterion(rid: str, crit: dict, on_progress: Optional[Callable[[int, int], None]] = None, run_id: Optional[str] = None) -> dict:
    """Re-read every proposal in the latest run and score it against the criterion's rubric. Cached by (rubric, file)."""
    props = _proposals_to_score(rid, run_id)
    st = L.load(rid)
    q = ""
    if crit.get("question_number"):
        qs = L.view(st)["questionnaire"]
        n = int(crit["question_number"])
        q = f"\nThe RFQ question this relates to: {qs[n - 1]}" if 0 < n <= len(qs) else ""
    instructions = (f"You are scoring ONE criterion for ONE vendor proposal against an RFQ.\nCriterion: {crit['name']}\n"
                    f"Rubric: {crit['rubric']}{q}\nScore 0-10 by the rubric (10 = fully meets it, 0 = absent or contradicts it). "
                    "Use only the vendor's document. Give a one-sentence reason and a short verbatim quote that supports the score "
                    "(null if there is nothing relevant). If the proposal says nothing relevant, score 0.")
    done, items = 0, {}
    lock = threading.Lock()

    def one(p):
        nonlocal done
        try:
            parsed, _ = llm.call("live_criterion", CRIT_PROMPT, CriterionScore, instructions, C.read_parts(C._upload_path(rid, st, p["n"])),
                                 doc_id=p["pid"], effort="low", max_output_tokens=4000)
            res = {"score": max(0.0, min(10.0, parsed.score)), "reason": parsed.reason, "quote": parsed.quote, "confidence": parsed.confidence}
        except Exception as e:
            res = {"score": 0.0, "reason": f"Could not be scored: {str(e)[:120]}", "quote": None, "confidence": "Low", "failed": True}
        with lock:
            items[L.org_key(p["org"])] = res
            done += 1
            if on_progress:
                on_progress(done, len(props))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, props))
    f = _scores_file(rid, crit)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"rubric_hash": rubric_hash(crit), "scored_at": iso(config.real_now()), "items": items}, ensure_ascii=False, indent=1))
    return items


def refresh_custom(rid: str, run_id: str) -> None:
    """During a run: score every custom rubric that has no current score for some proposal. Unchanged ones are cache hits."""
    sc = load_version(rid)
    if not sc:
        return
    props = _proposals_to_score(rid, run_id)
    for c in sc["criteria"]:
        if (c.get("rubric") or "").strip() and needs_scoring(rid, c, props):
            score_criterion(rid, c, run_id=run_id)


def append_chat(rid: str, text: str) -> None:
    f = C.adir(rid) / "chat.json"
    chat = json.loads(f.read_text()) if f.exists() else {"messages": [], "chat": []}
    chat["chat"].append({"role": "assistant", "text": text})
    C.adir(rid).mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(chat, ensure_ascii=False, indent=1))


def announce(rid: str, sc: dict, run_id: str) -> None:
    """Tell the buyer, in the analyst chat, which starting scorecard was set and how to change it."""
    f = C.adir(rid) / "chat.json"
    chat = json.loads(f.read_text()) if f.exists() else {"messages": [], "chat": []}
    lines = "\n".join(f"- **{c['name']}**: {c['weight']:g}, {c.get('reason') or ''}" for c in sc["criteria"])
    chat["chat"].append({"role": "assistant", "run_id": run_id, "text":
        f"I've set a starting scorecard (version {sc['version']}) from this RFQ. Weights out of 100:\n\n{lines}\n\n"
        "Tell me what matters more or less, for example “delivery is twice as important as warranty”, or add a criterion such as "
        "“score delivery plans 10 only if they name the origin and a date per site”."})
    C.adir(rid).mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(chat, ensure_ascii=False, indent=1))


def rebuild(rid: str) -> Optional[dict]:
    """Recompute the latest run's saved tables from saved data (no model calls). Returns headline ranking or None."""
    run = C.latest(rid)
    if not run:
        return None
    return C.rebuild_tables(rid, run)


def execute_pending(rid: str, pid: str, on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    with _lock:
        items = load_pending(rid)
        pend = next((p for p in items if p["id"] == pid), None)
        if not pend:
            raise ValueError("No such pending change")
        if pend["status"] != "pending":
            raise ValueError(f"That change is already {pend['status']}")
        pend["status"] = "approved"
        save_pending(rid, items)
    crit = pend["criterion"]
    try:
        score_criterion(rid, crit, on_progress)
        sc, _ = ensure_standard(rid)
        crits = place_criterion(sc["criteria"], crit)
        v = save_version(rid, crits, "buyer", f"{crit['name']}: rubric set, {pend['proposals']} proposals re-read")
        status = "applied"
    except Exception:
        with _lock:
            items = load_pending(rid)
            for p in items:
                if p["id"] == pid:
                    p["status"] = "failed"
            save_pending(rid, items)
        raise
    with _lock:
        items = load_pending(rid)
        for p in items:
            if p["id"] == pid:
                p["status"] = status
        save_pending(rid, items)
    return {"version": v["version"], "ranking": rebuild(rid)}


def cancel_pending(rid: str, pid: str) -> None:
    with _lock:
        items = load_pending(rid)
        for p in items:
            if p["id"] == pid and p["status"] == "pending":
                p["status"] = "cancelled"
        save_pending(rid, items)


def remove_criterion(rid: str, ref: str) -> dict:
    sc, _ = ensure_standard(rid)
    c = find(sc, ref)
    if len(sc["criteria"]) <= 1:
        raise ValueError("A scorecard needs at least one criterion")
    return save_version(rid, [x for x in sc["criteria"] if x["id"] != c["id"]], "buyer", f"Removed {c['name']}")


def restore(rid: str, n: int) -> dict:
    """Make an earlier version current again, as a new version (history is never rewritten)."""
    old = load_version(rid, n)
    if not old:
        raise ValueError(f"No scorecard version {n}")
    if current_version(rid) == n:
        raise ValueError("That version is already current")
    return save_version(rid, old["criteria"], "buyer", f"Restored version {n}")


def typed_approval_ok(pend: dict, user_text: str, user_msgs: int) -> bool:
    """A typed 'yes' counts only if it came after the proposal and is plainly affirmative."""
    return pend["status"] == "pending" and user_msgs > int(pend.get("user_msgs", 0)) and bool(AFFIRM.match(user_text or ""))


# ---- API ---------------------------------------------------------------------------------------------

def overview(rid: str) -> dict:
    sc = load_version(rid)
    return {"version": sc["version"] if sc else 0, "author": sc and sc["author"], "note": sc and sc["note"], "created_at": sc and sc["created_at"],
            "criteria": sc["criteria"] if sc else [], "versions": versions(rid),
            "pending": [p for p in load_pending(rid) if p["status"] == "pending"]}


@router.get("/api/live/rfqs/{rid}/scorecard")
def get_scorecard(rid: str):
    L.load(rid)
    return overview(rid)


@router.get("/api/live/rfqs/{rid}/scorecard/{n}")
def get_version(rid: str, n: int):
    L.load(rid)
    v = load_version(rid, n)
    if not v:
        raise HTTPException(404, "No such scorecard version")
    return v


@router.post("/api/live/rfqs/{rid}/scorecard/{n}/restore")
def restore_version(rid: str, n: int):
    L.load(rid)
    try:
        restore(rid, n)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    rebuild(rid)
    return overview(rid)


@router.get("/api/live/rfqs/{rid}/runs/{run_id}/scorecard/{n}")
def view_version(rid: str, run_id: str, n: int):
    """The comparison as it would rank under an earlier scorecard version. Computed on the fly; saves nothing."""
    L.load(rid)
    v = load_version(rid, n)
    if not v:
        raise HTTPException(404, "No such scorecard version")
    m = C.read_manifest(rid, run_id)
    tables, _ = C.compute(rid, run_id, m["settings"], scorecard_version=n)
    cols = [c for c in ("Rank", "Vendor", "Compliance Status", "Score / 100", "Fail Reasons") if c in tables["vendor_summary"].columns]
    return {"scorecard": v, "vendor_summary": C.records(tables["vendor_summary"][cols]), "scorecard_table": C.records(tables["scorecard"])}


@router.post("/api/live/rfqs/{rid}/analyst/pending/{pid}/cancel")
def cancel(rid: str, pid: str):
    L.load(rid)
    cancel_pending(rid, pid)
    return overview(rid)


@router.post("/api/live/rfqs/{rid}/analyst/pending/{pid}/approve")
def approve(rid: str, pid: str):
    L.load(rid)
    pend = next((p for p in load_pending(rid) if p["id"] == pid), None)
    if not pend or pend["status"] != "pending":
        raise HTTPException(409, "That change is no longer pending")
    q: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def work():
        try:
            res = execute_pending(rid, pid, lambda d, t: q.put(("progress", {"done": d, "total": t})))
            top = "; ".join(f"{r['Rank']}. {r['Vendor']} ({r.get('Score / 100')})" for r in (res.get("ranking") or [])[:3])
            append_chat(rid, f"Done: **{pend['criterion']['name']}** is scored for {pend['proposals']} proposals (scorecard version {res['version']}). "
                             f"Top by score: {top}.")
            q.put(("applied", res))
        except Exception as e:
            q.put(("error", {"message": str(e)[:300]}))
        q.put(("end", {}))
    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            ev, data = q.get()
            yield f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            if ev == "end":
                break

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
