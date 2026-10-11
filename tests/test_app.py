"""Route smoke test and a check that nothing from the old prototype is referenced."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_health_and_pages(state_dir):
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/health/details").json()["catalog_ready"] is True
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
        for old in ("store", "models", "seed", "audit", "pdf"):
            assert not re.search(rf"from \. import .*\b{old}\b|from \.{old} import", f.read_text()), (f.name, old)
