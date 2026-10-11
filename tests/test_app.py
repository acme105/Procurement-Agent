"""Route smoke test and a check that nothing from the old prototype is referenced."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_health_and_pages(state_dir):
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/health").json()["catalog_ready"] is True
    assert c.get("/web/").status_code == 200
    assert c.get("/api/live/current").json()["status"] == "DRAFT"
    assert c.get("/api/live/rfqs").json() == []


def test_frontend_has_no_old_references():
    files = [*ROOT.glob("web/*.js"), *ROOT.glob("web/views/*.js"), ROOT / "web" / "index.html"]
    text = "\n".join(f.read_text() for f in files)
    for old in ("/api/event", "/api/me", "/api/users", "X-User", "RFQ-2026-0147", "Corrugated", "ROLE_LABEL", "currentUser"):
        assert old not in text, old
    imports = set(re.findall(r"from '(\./|\.\./)([\w-]+)\.js'", text))
    for _, name in imports:
        assert list(ROOT.glob(f"web/{name}.js")) or list(ROOT.glob(f"web/views/{name}.js")), name


def test_no_old_modules_imported():
    for f in (ROOT / "app").glob("*.py"):
        for old in ("store", "models", "seed", "auth", "audit", "pdf"):
            assert not re.search(rf"from \. import .*\b{old}\b|from \.{old} import", f.read_text()), (f.name, old)


def test_buyer_login(state_dir, monkeypatch):
    import base64
    from app.main import app
    monkeypatch.setenv("AERCHAIN_BUYER_PASSWORD", "s3cret")
    c = TestClient(app)
    assert c.get("/api/live/current").status_code == 401
    assert c.get("/web/").status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"buyer:s3cret").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"buyer:nope").decode()}
    assert c.get("/api/live/current", headers=good).status_code == 200
    assert c.get("/api/live/current", headers=bad).status_code == 401
    assert c.get("/q/not-a-real-token").status_code == 404          # vendor page is open (the token is the key)
    assert c.post("/api/q/not-a-real-token/upload").status_code in (404, 422)
