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


def test_failure_names_missing_and_unrequested_items(state_dir):
    st = L.create_rfq()
    st["rfx"] = {"scope": "Test", "questionnaire": ["Warranty?"], "items": [
        {"item_codes": [], "generic_requirements": "Laptop 13-inch", "location": "Delhi", "quantity": 10},
        {"item_codes": [], "generic_requirements": "USB-C docking station", "location": "Delhi", "quantity": 5},
        {"item_codes": [], "generic_requirements": "USB-C docking station", "location": "Mumbai", "quantity": 5}]}
    st["status"] = "FINAL"
    L.save(st)
    rid = st["id"]

    def row(line, item, loc="Delhi", qty=10):
        return {"rfq_line": line, "item_description": item, "location": loc, "quantity": qty, "uom": "Each",
                "unit_price": 100.0, "currency": "INR", "extraction_confidence": "High"}
    p = _prop("individual", [_qa(9)])
    p["line_items"] = [row(1, "Laptop 13-inch"), row(None, "Laptop backpack"), row(None, "Laptop 15-inch", qty=10),
                       row(None, "Laptop 13-inch", loc="Kolkata")]      # extra location of a requested item: flag, not fail
    run = C.adir(rid) / "R-001"
    (run / "extractions").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"run_id": "R-001", "status": "done", "proposals": []}))
    (run / "extractions" / "P001.json").write_text(json.dumps({"pid": "P001", "org": "Apex", "filename": "f", "proposal": p}))
    tables, _ = C.compute(rid, "R-001", C.DEFAULTS)
    v = tables["vendor_summary"].iloc[0]
    assert v["Compliance Status"] == "FAIL"
    assert "no quote for USB-C docking station (lines 2–3)" in v["Fail Reasons"]
    assert "Quoted items not in the RFQ: Laptop backpack (1 rows); Laptop 15-inch (1 rows)" in v["Fail Reasons"]
    assert v["Missing Items"] == "USB-C docking station (lines 2–3)"
    assert "Kolkata" in v["Flags"] and "Kolkata" not in v["Fail Reasons"]


def test_several_catalog_codes_become_separate_lines(state_dir):
    st = L.create_rfq()
    out = L.apply_tool(st, "add_to_rfx", {"item_codes": ["IT-LAP-0001", "IT-LAP-0002", "IT-LAP-0003"], "site_quantities": {"Manyata": 5}})
    assert len(st["rfx"]["items"]) == 3 and out.endswith("[1, 2, 3]")
    assert [i["item_codes"] for i in st["rfx"]["items"]] == [["IT-LAP-0001"], ["IT-LAP-0002"], ["IT-LAP-0003"]]
    assert all(i["location"] == "Manyata" and i["quantity"] == 5 for i in st["rfx"]["items"])
    # codes together with a requirement text stay one line; a free-text item is one line
    L.apply_tool(st, "add_to_rfx", {"item_codes": ["A", "B"], "generic_requirements": "either is fine", "site_quantities": {"Manyata": 1}})
    L.apply_tool(st, "add_to_rfx", {"generic_requirements": "USB 3.0 pen drive 8 GB", "site_quantities": {"Manyata": 20, "Annexe": 10}})
    assert len(st["rfx"]["items"]) == 3 + 1 + 2
