import json
import time

from fastapi.testclient import TestClient

from app import visits
from app.main import app

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"


def _reset(monkeypatch, tmp_path, to="me@example.com"):
    monkeypatch.setattr(visits.config, "STATE_DIR", tmp_path)
    if to:
        monkeypatch.setenv("AERCHAIN_VISIT_ALERT_TO", to)
    else:
        monkeypatch.delenv("AERCHAIN_VISIT_ALERT_TO", raising=False)
    monkeypatch.delenv("AERCHAIN_BUYER_PASSWORD", raising=False)
    visits._pending.clear(); visits._last_sent.clear(); visits._sent_times.clear(); visits._held_back = 0
    sent = []
    monkeypatch.setattr(visits, "send_alert", lambda *a: sent.append(a))
    monkeypatch.setattr(visits, "reverse_dns", lambda ip: "host.example")
    return sent


def test_parse_ua():
    d = visits.parse_ua(UA)
    assert d["os"] == "iOS" and d["device"] == "mobile" and not d["bot"]
    assert visits.parse_ua("curl/8.0")["bot"] and visits.parse_ua("")["bot"]


def test_mask_token():
    assert visits.mask("/q/AbCdEfGhIjKl") == "/q/AbCd…"


def test_visit_logged_beacon_merged_and_emailed(monkeypatch, tmp_path):
    sent = _reset(monkeypatch, tmp_path)
    c = TestClient(app)
    h = {"user-agent": UA, "x-real-ip": "203.0.113.9"}
    assert c.get("/login", headers=h).status_code == 200
    assert c.post("/api/visit/beacon", json={"screen": "390x844", "timezone": "Asia/Kolkata"}, headers=h).status_code == 200
    time.sleep(0.5)
    assert len(sent) == 1
    d = sent[0][1]
    assert d["ip"] == "203.0.113.9" and d["device_info"]["timezone"] == "Asia/Kolkata"
    assert json.loads((tmp_path / "visits.jsonl").read_text().splitlines()[0])["ip"] == "203.0.113.9"
    subject, body = visits.render(d, 0, 1)
    assert "203.0.113.9" in subject and "Asia/Kolkata" in body


def test_debounce_and_bot_and_no_recipient(monkeypatch, tmp_path):
    sent = _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(visits, "BEACON_WAIT", 0.05)
    c = TestClient(app)
    h = {"user-agent": UA, "x-real-ip": "203.0.113.9"}
    for _ in range(3):
        c.get("/login", headers=h); time.sleep(0.3)
    assert len(sent) == 1                                         # same visitor within 30 minutes
    c.get("/login", headers={"user-agent": "curl/8", "x-real-ip": "198.51.100.1"}); time.sleep(0.3)
    assert len(sent) == 1                                         # bots are logged, not emailed
    assert len((tmp_path / "visits.jsonl").read_text().splitlines()) == 4
    sent2 = _reset(monkeypatch, tmp_path, to=None)
    c.get("/login", headers={"user-agent": UA, "x-real-ip": "203.0.113.50"}); time.sleep(0.3)
    assert sent2 == []                                            # no env var, no mail


def test_hourly_cap(monkeypatch, tmp_path):
    sent = _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(visits, "BEACON_WAIT", 0.02)
    c = TestClient(app)
    for i in range(visits.MAX_PER_HOUR + 3):
        c.get("/login", headers={"user-agent": UA, "x-real-ip": f"203.0.113.{i}"})
    time.sleep(0.8)
    assert len(sent) == visits.MAX_PER_HOUR and visits._held_back == 3


def test_login_failure_noted_without_password(monkeypatch, tmp_path):
    sent = _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(visits, "BEACON_WAIT", 0.05)
    monkeypatch.setenv("AERCHAIN_BUYER_PASSWORD", "right-pass")
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/auth/login", json={"password": "wrong-guess"}, headers={"user-agent": UA, "x-real-ip": "203.0.113.7"})
    assert r.status_code == 401
    time.sleep(0.3)
    assert sent and "login_failed" in sent[0][1]["events"]
    assert "wrong-guess" not in json.dumps(sent[0][1]) and "wrong-guess" not in (tmp_path / "visits.jsonl").read_text()
