"""Phase 1 — security prerequisite 18-A: superuser-only PB rules,
session-vs-data client split, and users.disabled enforcement.

Covers acceptance criteria AC7 (disabled account is logged out and cannot
log in) and the rules half of AC10 (PB REST must not be a weaker second
authorization layer).
"""

from __future__ import annotations

import inspect
import json
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import default_unique_fields
from tests.test_auth_http import _AuthFakePocketBase

RULE_KEYS = ("listRule", "viewRule", "createRule", "updateRule", "deleteRule")
COL_PARAMS = ("list_rule", "view_rule", "create_rule", "update_rule", "delete_rule")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 18-A(a): API rules are superuser-only everywhere
# ---------------------------------------------------------------------------
def test_col_defaults_are_superuser_only():
    from app.scripts.bootstrap_pb import col

    sig = inspect.signature(col)
    for param in COL_PARAMS:
        assert sig.parameters[param].default == "", f"{param} default is not ''"


def test_all_bootstrap_collections_are_superuser_only():
    from app.scripts.bootstrap_pb import COLLECTIONS

    assert len(COLLECTIONS) >= 18
    for spec in COLLECTIONS:
        for key in RULE_KEYS:
            assert spec.get(key) == "", f"{spec['name']}.{key} is not superuser-only"


def test_import_json_rules_are_superuser_only():
    data = json.loads((REPO_ROOT / "pb_collections_import.json").read_text())
    assert data, "import file has no collections"
    for coll in data:
        for key in RULE_KEYS:
            assert coll.get(key) == "", f"{coll['name']}.{key} is not superuser-only"


# ---------------------------------------------------------------------------
# is_disabled: works with both Record models and dicts
# ---------------------------------------------------------------------------
def test_is_disabled_handles_record_and_dict_shapes():
    from app.api.deps import is_disabled

    assert is_disabled(None) is False
    assert is_disabled({}) is False
    assert is_disabled({"id": "x"}) is False
    assert is_disabled({"disabled": False}) is False
    assert is_disabled({"disabled": True}) is True

    class Record:
        disabled = True

    assert is_disabled(Record()) is True

    class NoField:
        pass

    assert is_disabled(NoField()) is False


# ---------------------------------------------------------------------------
# Shared fake with a disabled user (18-A(c))
# ---------------------------------------------------------------------------
@pytest.fixture()
def pb(monkeypatch):
    fake = _AuthFakePocketBase(default_unique_fields())
    fake.collection("users").create(
        {
            "id": "u1",
            "email": "ok@x.com",
            "password": "pw123456",
            "role": "member",
            "displayName": "\u06a9\u0627\u0631\u0628\u0631 \u0641\u0639\u0627\u0644",
            "disabled": False,
        }
    )
    fake.collection("users").create(
        {
            "id": "u2",
            "email": "off@x.com",
            "password": "pw123456",
            "role": "member",
            "displayName": "\u06a9\u0627\u0631\u0628\u0631 \u063a\u06cc\u0631\u0641\u0639\u0627\u0644",
            "disabled": True,
        }
    )
    monkeypatch.setattr("app.middleware.get_pb", lambda: fake)
    monkeypatch.setattr("app.middleware.get_data_pb", lambda: fake)
    monkeypatch.setattr("app.api.auth.get_pb", lambda: fake)
    return fake


def test_disabled_user_with_valid_session_is_logged_out(pb):
    """AC7a: a still-valid token for a disabled account → 303 to the
    disabled notice, and the cookie is dropped so requests stop re-authing."""
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "tok-u2")
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?disabled=1"
        # dead cookie dropped (header check — httpx's jar keeps manually-set
        # cookies, same quirk test_logout_clears_cookie works around)
        assert "Max-Age=0" in resp.headers.get("set-cookie", "")
        # no bounce loop: the notice page itself is public
        follow = client.get("/login?disabled=1")
        assert follow.status_code == 200
        assert 'data-toast-type="warning"' in follow.text
        assert 'class="alert' not in follow.text
        assert "deactivated" in follow.text


def test_login_refuses_disabled_account_native(pb):
    """AC7b: native form POST → Persian error page, no session cookie."""
    with TestClient(app) as client:
        resp = client.post("/login", data={"email": "off@x.com", "password": "pw123456"})
        assert resp.status_code == 200
        assert 'data-toast-type="error"' in resp.text
        assert 'class="alert' not in resp.text
        assert "Your account is disabled" in resp.text
        assert "pb_auth" not in resp.headers.get("set-cookie", "")
        # still anonymous afterwards
        assert client.get("/projects", follow_redirects=False).status_code == 303


def test_login_refuses_disabled_account_htmx(pb):
    with TestClient(app) as client:
        resp = client.post(
            "/login",
            data={"email": "off@x.com", "password": "pw123456"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        events = json.loads(resp.headers.get("hx-trigger", "{}"))
        assert "disabled" in events["show-toast"]["message"]
        assert "pb_auth" not in resp.headers.get("set-cookie", "")


def test_enabled_user_login_still_works(pb):
    """Regression: the disabled check must not break normal login."""
    with TestClient(app) as client:
        resp = client.post(
            "/login",
            data={"email": "ok@x.com", "password": "pw123456"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "pb_auth=tok-u1" in resp.headers.get("set-cookie", "")
        assert client.get("/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# 18-A(b): routes read through the data client, lazily
# ---------------------------------------------------------------------------
def test_authenticated_routes_resolve_data_client(pb, monkeypatch):
    calls = {"n": 0}

    def counting_get_data_pb() -> Any:
        calls["n"] += 1
        return pb

    monkeypatch.setattr("app.middleware.get_data_pb", counting_get_data_pb)
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "tok-u1")
        resp = client.get("/projects")
        assert resp.status_code == 200
    assert calls["n"] >= 1, "route did not resolve request.state.pb via get_data_pb"


def test_public_pages_never_touch_data_client(monkeypatch):
    """Laziness: static/login/health requests must not force superuser auth
    (so a PB outage never breaks the login page or static assets)."""

    def explode() -> Any:
        raise AssertionError("data client resolved on a public request")

    monkeypatch.setattr("app.middleware.get_data_pb", explode)
    with TestClient(app) as client:
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in to EzDistro" in resp.text


def test_session_validation_never_clobbers_data_client(pb, monkeypatch):
    """The user's session token must never land on the shared superuser
    client (that would clobber the admin auth store)."""
    from tests.test_auth_http import _AuthFakePocketBase

    session_pb = _AuthFakePocketBase(default_unique_fields())
    session_pb.collection("users").create(
        {"id": "u1", "email": "ok@x.com", "password": "pw123456", "role": "member"}
    )
    monkeypatch.setattr("app.middleware.get_pb", lambda: session_pb)
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "tok-u1")
        assert client.get("/dashboard").status_code == 200
    assert session_pb.auth_store.token == "tok-u1"
    assert pb.auth_store.token == ""
    assert pb.auth_store.model is None
