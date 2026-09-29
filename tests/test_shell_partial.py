"""Application shell contract: the sidebar/header/scripts render once, and
HTMX navigation swaps only the page body.

Guards the persistent-shell behaviour in base.html / layouts/platform.html and
the HTMX auth redirect in middleware.py — a boosted request must never inject
the login page into #main-content.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.templates import templates
from tests.helpers import make_pb, make_req, make_user

client = TestClient(app)


def _render(hx: bool) -> str:
    req = make_req(make_pb(), make_user(), "p1")
    if not hx:
        req.headers = {}
    resp = templates.TemplateResponse(
        req, "pages/articles/render_error.html", {"project": {"id": "p1"}, "message": "x"}
    )
    return resp.body.decode()


def test_htmx_request_renders_page_body_without_shell():
    body = _render(hx=True)
    assert "<aside" not in body
    assert "<!doctype" not in body
    assert "app.js" not in body
    assert "toast-container" not in body
    assert "ezdistro-panel" in body


def test_plain_request_renders_full_shell():
    body = _render(hx=False)
    assert '<aside class="ezdistro-sidebar"' in body
    assert "<!doctype" in body
    assert "toast-container" in body


def test_unauthenticated_htmx_request_redirects_via_header():
    resp = client.get("/dashboard", headers={"HX-Request": "true"}, follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/login"
    assert resp.text == ""


def test_unauthenticated_plain_request_still_redirects():
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
