"""Sign-in: one shared password, a session cookie, sign-out, and what stays open."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app

HTTPS = {"X-Forwarded-Proto": "https"}


@pytest.fixture()
def client(state_dir, monkeypatch):
    monkeypatch.setenv("AERCHAIN_BUYER_PASSWORD", "s3cret")
    return TestClient(app, base_url="http://testserver")


def sign_in(c, pw="s3cret", **kw):
    return c.post("/api/auth/login", json={"password": pw}, **kw)


def test_login_is_off_without_a_password(state_dir, monkeypatch):
    monkeypatch.delenv("AERCHAIN_BUYER_PASSWORD", raising=False)
    c = TestClient(app)
    assert c.get("/api/live/current").status_code == 200
    assert c.get("/api/auth/me").json() == {"signed_in": True, "login_required": False}


def test_what_stays_open_and_what_is_protected(client):
    assert client.get("/login").status_code == 200 and "Sign in" in client.get("/login").text
    assert client.get("/web/").status_code == 200 and client.get("/web/logo.svg").status_code == 200
    assert client.get("/api/health").json() == {"ok": True}                 # nothing about the configuration
    assert client.get("/q/not-a-token").status_code == 404                  # the vendor page is open (the token is the key)
    r = client.get("/api/live/current")
    assert r.status_code == 401 and r.json() == {"detail": "Sign in required", "reason": "missing"}
    assert client.get("/api/health/details").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_errors_on_sign_in(client):
    assert sign_in(client, "").status_code == 400 and sign_in(client, "").json()["detail"] == "Enter your password."
    r = sign_in(client, "wrong")
    assert r.status_code == 401 and r.json()["detail"] == "That password isn't right."
    assert auth.COOKIE not in r.cookies
    assert client.get("/api/live/current").status_code == 401


def test_sign_in_sets_a_safe_cookie_and_opens_the_app(state_dir, monkeypatch):
    monkeypatch.setenv("AERCHAIN_BUYER_PASSWORD", "s3cret")
    client = TestClient(app, base_url="https://testserver")                  # a Secure cookie is only sent back over https
    r = sign_in(client, headers=HTTPS)
    assert r.status_code == 200 and r.json() == {"ok": True, "next": "/web/#/live"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie and "max-age=31536000" in cookie
    assert client.get("/api/live/current", headers=HTTPS).status_code == 200
    assert client.get("/api/health/details", headers=HTTPS).json()["ok"] is True
    assert client.get("/api/auth/me", headers=HTTPS).json() == {"signed_in": True, "login_required": True}
    assert client.get("/login", follow_redirects=False, headers=HTTPS).status_code in (302, 307)   # already signed in


def test_sessions_are_stored_hashed(client, state_dir):
    sign_in(client)
    token = client.cookies.get(auth.COOKIE)
    stored = (state_dir / "sessions.json").read_text()
    assert token not in stored and auth._hash(token) in stored


def test_sign_out_ends_the_session_on_the_server(client):
    sign_in(client)
    token = client.cookies.get(auth.COOKIE)
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set(auth.COOKIE, token)                                   # replaying the old cookie must not work
    r = client.get("/api/live/current")
    assert r.status_code == 401 and r.json()["reason"] == "expired"


def test_a_changed_password_signs_everyone_out(client, monkeypatch):
    sign_in(client)
    assert client.get("/api/live/current").status_code == 200
    monkeypatch.setenv("AERCHAIN_BUYER_PASSWORD", "new-password")
    assert client.get("/api/live/current").status_code == 401
    assert sign_in(client, "s3cret").status_code == 401 and sign_in(client, "new-password").status_code == 200


def test_requests_from_other_sites_are_refused(client):
    r = client.post("/api/auth/login", json={"password": "s3cret"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = client.post("/api/auth/login", json={"password": "s3cret"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200
    r = client.post("/api/live/rfqs", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403                                              # even with a valid session


def test_next_only_accepts_paths_on_this_site():
    assert auth.safe_next("/web/#/compare-live?id=RFQ-L-002") == "/web/#/compare-live?id=RFQ-L-002"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", "", None, "/x://y"):
        assert auth.safe_next(bad) == "/web/#/live", bad


def test_failed_attempts_are_logged_without_the_password(client, state_dir):
    sign_in(client, "wrong-guess-123")
    log = (state_dir / "auth.log").read_text()
    assert "login_failed" in log and "wrong-guess-123" not in log


def test_the_old_basic_prompt_is_gone(client):
    assert "www-authenticate" not in {k.lower() for k in client.get("/api/live/current").headers}
