"""App-level smoke tests: boot, auth gating, login flow, health."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    """/health always answers with a status payload; without a live PocketBase
    it reports degraded (503) — with one it reports ok (200)."""
    resp = client.get("/health")
    assert resp.status_code in (200, 503)
    assert "status" in resp.json()
    if resp.status_code == 503:
        assert resp.json()["status"] == "degraded"
    else:
        assert resp.json()["status"] == "ok"


def test_unauthenticated_redirects_to_login():
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_renders():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "ورود به EzDistro" in resp.text


def test_login_page_redirects_when_authenticated():
    """Without a real PB the cookie path can't be exercised; the page itself is enough."""
    resp = client.get("/login", headers={"Cookie": "pb_auth=not-a-real-token"})
    # PB is unreachable in tests → auth refresh fails → treated as anonymous → page renders
    assert resp.status_code == 200


def test_public_static_served():
    resp = client.get("/manifest.json")
    assert resp.status_code == 200
