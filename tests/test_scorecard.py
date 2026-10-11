"""Scorecard: formulas, weights and relations, gates, ranking, and the approval rules for re-reading. The model is stubbed."""
import json

import pytest

from app import live_analyst as A, live_compare as C, live_rfx as L, live_scorecard as SC, llm


def _setup(vendors):
    """A final RFQ (1 line, 2 questions) and one finished run with the given extractions: {org: (price, lead, [q1, q2] or None)}."""
    st = L.create_rfq()
    st["rfx"] = {"scope": "Test", "items": [{"item_codes": [], "generic_requirements": "Mouse", "location": "Delhi", "quantity": 10}],
                 "questionnaire": ["Delivery plan?", "Warranty?"]}
    st["status"] = "FINAL"
    L.save(st)
    rid = st["id"]
    run = C.adir(rid) / "R-001"
    (run / "extractions").mkdir(parents=True)
    props = []
    for i, (org, (price, lead, qs)) in enumerate(vendors.items(), 1):
        answers = [{"question_number": n, "question": f"q{n}", "answer": f"answer {n}", "confidence_score": s, "confidence_reasoning": "r"}
                   for n, s in enumerate(qs, 1)] if qs else []
        p = {"vendor_name": org, "line_items": [{"rfq_line": 1, "item_description": "Mouse", "location": "Delhi", "quantity": 10, "uom": "Each",
                                                  "unit_price": price, "currency": "INR", "extraction_confidence": "High"}],
             "questionnaire_style": "individual" if qs else "none", "questionnaire_answers": answers, "lead_time_days": lead,
             "payment_terms": None, "freight_included": None, "bundle_conditions": None, "special_notes": None, "discount_percentage": 0.0}
        (run / "extractions" / f"P{i:03d}.json").write_text(json.dumps({"pid": f"P{i:03d}", "org": org, "filename": "f", "proposal": p}))
        props.append({"pid": f"P{i:03d}", "n": i, "org": org, "status": "done"})
    (run / "manifest.json").write_text(json.dumps({"run_id": "R-001", "status": "done", "settings": C.DEFAULTS, "proposals": props}))
    (C.adir(rid) / "latest.txt").write_text("R-001")
    return rid


@pytest.fixture()
def equal_weights(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no model in tests")
    monkeypatch.setattr(llm, "call", boom)         # the standard proposal falls back to equal weights


VENDORS = {"Cheap Slow": (100.0, 20, [9, 4]), "Dear Fast": (200.0, 10, [6, 9]), "Silent": (150.0, 15, None)}


def _summary(rid):
    tables, _ = C.compute(rid, "R-001", C.DEFAULTS)
    return tables["vendor_summary"].set_index("Vendor"), tables["scorecard"]


def test_standard_scorecard_falls_back_to_equal_weights(state_dir, equal_weights):
    rid = _setup(VENDORS)
    sc, created = SC.ensure_standard(rid)
    assert created and [c["id"] for c in sc["criteria"]] == ["price", "lead_time", "q1", "q2"]
    assert all(c["weight"] == 25 for c in sc["criteria"]) and "Equal weights" in sc["note"]
    assert SC.ensure_standard(rid)[1] is False


def test_model_weights_are_validated_and_normalised(state_dir, monkeypatch):
    rid = _setup(VENDORS)
    ok = SC.WeightProposal(criteria=[SC.WeightItem(id=i, weight=w, reason="r") for i, w in (("price", 30), ("lead_time", 30), ("q1", 20), ("q2", 40))])
    monkeypatch.setattr(llm, "call", lambda *a, **k: (ok, None))
    sc, _ = SC.ensure_standard(rid)
    assert [c["weight"] for c in sc["criteria"]] == [25.0, 25.0, 16.67, 33.33]


def test_scores_totals_and_ranking(state_dir, equal_weights):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    s, tbl = _summary(rid)
    # price: lowest cost among complete quotes scores 10, others in proportion; lead time likewise
    assert tbl[(tbl.Vendor == "Dear Fast") & (tbl["Criterion Id"] == "price")]["Score"].iloc[0] == 5.0
    assert tbl[(tbl.Vendor == "Cheap Slow") & (tbl["Criterion Id"] == "lead_time")]["Score"].iloc[0] == 5.0
    # Cheap Slow: price 10, lead 5, q 9, q 4 -> 25*(10+5+9+4)/10 = 70.0; Dear Fast: 5, 10, 6, 9 -> 75.0
    assert s.loc["Cheap Slow", "Score / 100"] == 70.0 and s.loc["Dear Fast", "Score / 100"] == 75.0
    assert s.loc["Dear Fast", "Rank"] == 1 and s.loc["Cheap Slow", "Cost rank"] == 1       # scorecard and cost views disagree
    assert s.loc["Silent", "Compliance Status"] == "FAIL"                                  # no answers still fails, ranked last
    assert s.index.tolist()[-1] == "Silent"


def test_weights_relations_and_contradictions(state_dir, equal_weights):
    rid = _setup(VENDORS)
    sc, _ = SC.ensure_standard(rid)
    v = SC.apply_weights(rid, None, [{"a": "lead_time", "b": "q2", "factor": 2}])
    w = {c["id"]: c["weight"] for c in v["criteria"]}
    assert w["lead_time"] == pytest.approx(2 * w["q2"], rel=0.01) and sum(w.values()) == pytest.approx(100, abs=0.1)
    v = SC.apply_weights(rid, {"price": 70}, None)
    w = {c["id"]: c["weight"] for c in v["criteria"]}
    assert w["price"] == 70 and sum(w.values()) == pytest.approx(100, abs=0.1)              # explicit numbers hold exactly
    assert w["lead_time"] == pytest.approx(2 * w["q2"], rel=0.02)                            # the earlier relation survives
    v = SC.apply_weights(rid, {"lead_time": 30}, [{"a": "q2", "b": "lead_time", "factor": 0.5}])
    w = {c["id"]: c["weight"] for c in v["criteria"]}
    assert w["lead_time"] == 30 and w["q2"] == pytest.approx(15, abs=0.1)                    # a relation to a fixed criterion fixes it
    with pytest.raises(ValueError):
        SC.apply_weights(rid, {"price": 80, "lead_time": 40}, None)                          # over 100
    with pytest.raises(ValueError):                                                          # A = 2B and B = 2A cannot both hold
        SC.apply_weights(rid, None, [{"a": "q1", "b": "q2", "factor": 2}, {"a": "q2", "b": "q1", "factor": 2}])
    with pytest.raises(ValueError):
        SC.apply_weights(rid, {"price": -5}, None)
    assert SC.current_version(rid) == 4                                                      # failed attempts saved nothing


def test_minimum_score_fails_a_vendor_with_a_named_reason(state_dir, equal_weights):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    SC.update(rid, lambda cs: [{**c, "min_score": 5} if c["id"] == "q1" else c for c in cs], "min")
    s, _ = _summary(rid)
    assert s.loc["Dear Fast", "Compliance Status"] == "PASS" and s.loc["Cheap Slow", "Compliance Status"] == "PASS"   # 6 and 9 both clear 5
    SC.update(rid, lambda cs: [{**c, "min_score": 7} if c["id"] == "q1" else c for c in cs], "min")
    s, _ = _summary(rid)
    assert s.loc["Dear Fast", "Compliance Status"] == "FAIL" and "Below minimum on Q1 (6.0 < 7)" in s.loc["Dear Fast", "Fail Reasons"]


def _stub_scoring(monkeypatch, calls):
    monkeypatch.setattr(C, "read_parts", lambda path: [])
    monkeypatch.setattr(C, "_upload_path", lambda rid, st, n: None)

    def fake(job, ver, schema, instr, parts, **k):
        calls.append(k.get("doc_id"))
        return SC.CriterionScore(score=9.0 if k.get("doc_id") == "P002" else 3.0, reason="stub", quote="q", confidence="High"), None
    monkeypatch.setattr(llm, "call", fake)


def test_new_rubric_needs_approval_and_nothing_is_read_until_then(state_dir, equal_weights, monkeypatch):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    calls: list = []
    _stub_scoring(monkeypatch, calls)
    res = SC.propose_criterion(rid, "Delivery plan", "10 if it names origin and dates", 15, None, None, user_msgs=1, exact=True)
    assert res["needs_approval"] and res["proposals"] == 3 and calls == []
    assert SC.current_version(rid) == 1                                                      # nothing applied yet
    out = SC.execute_pending(rid, res["pending_id"])
    assert sorted(calls) == ["P001", "P002", "P003"] and out["version"] == 2
    s, tbl = _summary(rid)
    assert "delivery_plan" in set(tbl["Criterion Id"])
    w = {c["id"]: c["weight"] for c in SC.load_version(rid)["criteria"]}
    assert w["delivery_plan"] == 15 and sum(w.values()) == pytest.approx(100, abs=0.1)         # a stated weight holds exactly
    assert tbl[(tbl.Vendor == "Dear Fast") & (tbl["Criterion Id"] == "delivery_plan")]["Score"].iloc[0] == 9.0
    # the same rubric again: weight change only, no scoring
    calls.clear()
    again = SC.propose_criterion(rid, "Delivery plan", "10 if it names origin and dates", 50, None, None, user_msgs=2)
    assert again["applied"] and calls == []
    with pytest.raises(ValueError):                                                          # already applied: can't run twice
        SC.execute_pending(rid, res["pending_id"])


def test_typed_approval_rules_and_the_analyst_guard(state_dir, equal_weights, monkeypatch):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    calls: list = []
    _stub_scoring(monkeypatch, calls)
    res = SC.propose_criterion(rid, "Delivery plan", "10 if it names origin and dates", 30, None, None, user_msgs=1)
    pend = SC.load_pending(rid)[0]
    assert not SC.typed_approval_ok(pend, "yes", 1)                    # no later message from the buyer
    assert not SC.typed_approval_ok(pend, "make it 3x", 2)             # not an approval
    assert SC.typed_approval_ok(pend, "Yes, go ahead", 2)
    refused = A.call_tool(rid, "R-001", "apply_pending", {"pending_id": res["pending_id"]}, [], {"user_text": "hmm, what about price?", "user_msgs": 2})
    assert refused.startswith("Refused") and calls == []
    ev: list = []
    done = A.call_tool(rid, "R-001", "apply_pending", {"pending_id": res["pending_id"]}, ev, {"user_text": "yes please", "user_msgs": 2})
    assert json.loads(done)["scorecard_version"] == 2 and len(calls) == 3 and ev[0][0] == "tables"


def test_cancel_and_remove(state_dir, equal_weights, monkeypatch):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    res = SC.propose_criterion(rid, "Support", "10 if 24x7 support", None, None, None, user_msgs=1)
    SC.cancel_pending(rid, res["pending_id"])
    assert SC.overview(rid)["pending"] == []
    v = SC.remove_criterion(rid, "lead_time")
    assert "lead_time" not in [c["id"] for c in v["criteria"]] and sum(c["weight"] for c in v["criteria"]) == pytest.approx(100, abs=0.1)


def test_failed_scoring_is_flagged_not_failed_and_is_retried(state_dir, equal_weights, monkeypatch):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    monkeypatch.setattr(C, "read_parts", lambda path: [])
    monkeypatch.setattr(C, "_upload_path", lambda rid, st, n: None)
    state = {"fail": True}

    def flaky(job, ver, schema, instr, parts, **k):
        if state["fail"] and k.get("doc_id") == "P002":
            raise TimeoutError("timed out")
        return SC.CriterionScore(score=8.0, reason="ok", quote=None, confidence="High"), None
    monkeypatch.setattr(llm, "call", flaky)
    res = SC.propose_criterion(rid, "Support", "10 if 24x7", 10, 5, None, user_msgs=1, exact=True)
    SC.execute_pending(rid, res["pending_id"])
    s, tbl = _summary(rid)
    assert "could not be scored" in s.loc["Dear Fast", "Flags"] and s.loc["Dear Fast", "Compliance Status"] == "PASS"   # flagged, not failed
    state["fail"] = False
    again = SC.propose_criterion(rid, "Support", "10 if 24x7", 10, 5, None, user_msgs=3, exact=True)   # same rubric, one proposal failed
    assert again["needs_approval"] and again["proposals"] == 1
    SC.execute_pending(rid, again["pending_id"])
    s, _ = _summary(rid)
    assert "could not be scored" not in str(s.loc["Dear Fast", "Flags"])


def test_estimate_is_longer_for_scans(state_dir, equal_weights):
    rid = _setup(VENDORS)
    st = L.load(rid)
    st["uploads"] = [{"n": 1, "filename": "a.png", "org": "x"}, {"n": 2, "filename": "b.docx", "org": "y"}, {"n": 3, "filename": "c.pdf", "org": "z"}]
    L.save(st)
    props = [{"n": 1}, {"n": 2}, {"n": 3}]
    assert SC.estimate_seconds(rid, props) >= 150 and SC.estimate_seconds(rid, [{"n": 2}]) <= 20


def test_versions_keep_their_own_scores_and_can_be_restored(state_dir, equal_weights, monkeypatch):
    rid = _setup(VENDORS)
    SC.ensure_standard(rid)
    monkeypatch.setattr(C, "read_parts", lambda path: [])
    monkeypatch.setattr(C, "_upload_path", lambda rid, st, n: None)
    level = {"s": 9.0}
    monkeypatch.setattr(llm, "call", lambda *a, **k: (SC.CriterionScore(score=level["s"], reason="r", quote=None, confidence="High"), None))
    SC.execute_pending(rid, SC.propose_criterion(rid, "Support", "10 if 24x7", 20, None, None, 1, exact=True)["pending_id"])        # v2
    level["s"] = 2.0
    SC.execute_pending(rid, SC.propose_criterion(rid, "Support", "10 only if 24x7 with a named engineer", 20, None, None, 3, exact=True)["pending_id"])  # v3
    assert SC.current_version(rid) == 3

    def support_scores(version):
        tables, _ = C.compute(rid, "R-001", C.DEFAULTS, scorecard_version=version)
        t = tables["scorecard"]
        return set(t[t["Criterion Id"] == "support"]["Score"])
    assert support_scores(2) == {9.0} and support_scores(3) == {2.0}                        # the old rubric keeps its old scores
    SC.restore(rid, 2)
    assert SC.current_version(rid) == 4 and SC.load_version(rid)["note"] == "Restored version 2"
    assert {c["id"]: c["rubric"] for c in SC.load_version(rid)["criteria"]}["support"] == "10 if 24x7"
    with pytest.raises(ValueError):
        SC.restore(rid, 4)                                                                    # already current
    with pytest.raises(ValueError):
        SC.restore(rid, 99)
