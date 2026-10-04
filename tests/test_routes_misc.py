"""Route tests — misc/infra: dashboard stats fragment, PWA manifest, service
worker, offline page, favicon, debug page (dev-only)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.api import dashboard as D
from app.routes import debug, pwa
from tests.helpers import make_pb, make_req, make_user


def test_dashboard_stats_partial_renders():
    pb = make_pb()
    req = make_req(pb, make_user("admin1", role="admin"), "")
    resp = D.dashboard_stats_partial(req)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert body.strip() != ""
    assert body.count('id="stats-row"') == 1


def test_manifest_json_is_valid_json():
    manifest = asyncio.run(pwa.dynamic_manifest())
    assert manifest.get("name")
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert len(manifest["icons"]) >= 1
    assert any(i.get("type") == "image/svg+xml" for i in manifest["icons"])


def test_service_worker_serves_js():
    resp = asyncio.run(pwa.service_worker())
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "serviceWorker" in body or "importScripts" in body or "CACHE" in body
    assert resp.headers.get("Service-Worker-Allowed") == "/"


def test_offline_page_renders():
    html = asyncio.run(pwa.offline_page())
    assert 'lang="en" dir="ltr"' in html
    assert "You're offline" in html


def test_favicon_redirects_to_icon():
    resp = asyncio.run(pwa.dynamic_favicon())
    assert resp.status_code == 307
    assert resp.headers["location"] == "/static/brand/ezdistro/favicon.ico"


def test_debug_page_dev_only():
    pb = make_pb()
    user = SimpleNamespace(id="admin1", role="admin", email="a@x.com")
    req = make_req(pb, user, "")
    payload = debug.debug_test(req)
    assert payload == {"user": "admin1"}


def test_login_page_renders():
    from app.api.auth import login_page

    pb = make_pb()
    req = make_req(pb, make_user(), "")
    req.state.user = None
    resp = login_page(req)
    assert resp.status_code == 200
    assert "Sign in to EzDistro" in resp.body.decode()


def test_openapi_schema_available_in_dev():
    from app.main import app as app_obj

    schema = app_obj.openapi()
    assert "paths" in schema
    assert "/projects/{project_id}/topics/import/preview" in schema["paths"]


def test_custom_docs_renders_swagger():
    from fastapi.testclient import TestClient

    from app.main import app as app_obj

    with TestClient(app_obj) as client:
        r = client.get("/docs")
        assert r.status_code == 200
        assert "swagger" in r.text.lower()
