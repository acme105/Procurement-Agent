"""No model calls: RFQ state, one file per organisation, finalise rules, compliance and the analyst's table tools."""
import json

import pandas as pd
import pytest
from fastapi import HTTPException

from app import live_analyst as A, live_compare as C, live_rfx as L


def _final_rfq():
    st = L.create_rfq()
    st["rfx"] = {"scope": "Test", "items": [{"item_codes": [], "generic_requirements": "Mouse", "location": "Delhi", "quantity": 10}],
                 "questionnaire": ["Warranty?"]}
    st["status"] = "FINAL"
    L.save(st)
    return st["id"]


def test_new_rfq_reuses_empty_draft(state_dir):
    a = L.current()
    assert is_same(a, L.current())
    assert L.is_empty(a)


def is_same(a, b):
    return a["id"] == b["id"]


def test_upload_replaces_same_organisation(state_dir):
    rid = _final_rfq()
    L.store_upload(rid, "Acme  Traders", None, "link", "q1.csv", b"a,b\n1,2\n")
    r = L.store_upload(rid, "acme traders", None, "link", "q2.csv", b"a,b\n3,4\n")
    assert r["replaced"] == ["q1.csv"]
    active = [u for u in L.load(rid)["uploads"] if not u.get("superseded")]
    assert [u["filename"] for u in active] == ["q2.csv"]


def test_upload_rules(state_dir):
    rid = _final_rfq()
    with pytest.raises(HTTPException):
        L.store_upload(rid, "", None, "link", "q.csv", b"x")                   # organisation required
    with pytest.raises(HTTPException):
        L.store_upload(rid, "Acme", None, "link", "q.exe", b"x")               # type not accepted
    draft = L.create_rfq()
    with pytest.raises(HTTPException):
        L.store_upload(draft["id"], "Acme", None, "link", "q.csv", b"x")       # RFQ not finalised


def test_finalise_needs_scope_and_lines(state_dir):
    st = L.create_rfq()
    with pytest.raises(HTTPException) as e:
        L.finalise(st["id"], request=None)
    assert e.value.status_code == 422


def _prop(style, answers, qty=10, price=100.0, line=1):
    return {"vendor_name": "x", "line_items": [{"rfq_line": line, "item_description": "Mouse", "location": "Delhi", "quantity": qty,
                                                "uom": "Each", "unit_price": price, "currency": "INR", "extraction_confidence": "High"}],
            "questionnaire_style": style, "questionnaire_answers": answers, "lead_time_days": 5, "payment_terms": "Net 30",
            "freight_included": True, "bundle_conditions": None, "special_notes": None, "discount_percentage": 0.0}


def _qa(score):
    return {"question_number": 1, "question": "Warranty?", "answer": "2 years", "confidence_score": score, "confidence_reasoning": "ok"}


def test_compliance_rules(state_dir):
    rid = _final_rfq()
    vendors = {"Individual Good": _prop("individual", [_qa(9)]), "Individual Weak": _prop("individual", [_qa(3)]),
               "Blanket": _prop("blanket", [{**_qa(10), "question_number": None}]), "Silent": _prop("none", []),
               "Short": _prop("individual", [_qa(9)], qty=4)}
    run = C.adir(rid) / "R-001"
    (run / "extractions").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"run_id": "R-001", "status": "done", "proposals": []}))
    for i, (org, p) in enumerate(vendors.items(), 1):
        (run / "extractions" / f"P{i:03d}.json").write_text(json.dumps({"pid": f"P{i:03d}", "org": org, "filename": "f", "proposal": p}))
    tables, summary = C.compute(rid, "R-001", C.DEFAULTS)
    s = tables["vendor_summary"].set_index("Vendor")
    assert s.loc["Individual Good", "Compliance Status"] == "PASS"
    assert s.loc["Individual Weak", "Fail Reasons"] == "Low QA score"
    assert s.loc["Blanket", "Compliance Status"] == "PASS" and "Blanket Q&A answer" in s.loc["Blanket", "Flags"]
    assert s.loc["Silent", "Fail Reasons"] == "No questionnaire answers"
    assert "Quantity shortfall" in s.loc["Short", "Fail Reasons"]
    assert summary["passing"] == 2


def test_query_table_tool():
    df = pd.DataFrame({"Vendor": ["A", "A", "B"], "Cost": [1.0, 2.0, 5.0], "Status": ["PASS", "PASS", "FAIL"]})
    out = A.query(df, {"filters": [{"column": "status", "op": "eq", "value": "pass"}], "group_by": ["Vendor"],
                       "aggregations": [{"column": "Cost", "func": "sum", "as": "Total"}]})
    assert out.to_dict("records") == [{"Vendor": "A", "Total": 3.0}]
    with pytest.raises(ValueError):
        A.query(df, {"columns": ["Nope"]})
