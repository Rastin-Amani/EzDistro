"""HTTP-level auth tests via TestClient (real middleware + cookies).

The PocketBase client is replaced at the module boundary (app.middleware.get_pb)
with an in-memory fake that supports the auth flows the middleware and login
route exercise: auth_with_password, auth_refresh, auth_store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pocketbase.errors import ClientResponseError

from app.main import app
from tests.fakes import FakePocketBase, default_unique_fields


class _FakeAuthStore:
    def __init__(self) -> None:
        self.token = ""
        self.model: Any = None

    def save(self, token: str, model: Any) -> None:
        self.token = token
        self.model = model

    def clear(self) -> None:
        self.token = ""
        self.model = None


class _AuthUsersService:
    """Duck-typed users collection: password auth against stored records."""

    def __init__(self, storage, auth_store: _FakeAuthStore) -> None:
        self._storage = storage
        self._auth = auth_store

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        from tests.fakes import FakeRecordService

        return FakeRecordService(self._storage, "users").create(data)

    def auth_with_password(self, identity: str, password: str) -> dict[str, Any]:
        for record in self._storage.records("users"):
            if str(record.get("email", "")).lower() == identity.strip().lower():
                if record.get("password") == password:
                    self._auth.token = f"tok-{record['id']}"
                    self._auth.model = dict(record)
                    return dict(record)
                break
        raise ClientResponseError("invalid credentials", status=400)

    def auth_refresh(self) -> dict[str, Any]:
        token = self._auth.token
        if token.startswith("tok-"):
            user_id = token[4:]
            for record in self._storage.records("users"):
                if record.get("id") == user_id:
                    self._auth.model = dict(record)
                    return dict(record)
        raise ClientResponseError("unauthorized", status=401)


class _AuthFakePocketBase(FakePocketBase):
    def __init__(self, unique_fields=None) -> None:
        super().__init__(unique_fields)
        self.auth_store = _FakeAuthStore()

    def collection(self, name: str):
        if name == "users":
            return _AuthUsersService(self.storage, self.auth_store)
        return super().collection(name)


@pytest.fixture()
def fake_pb(monkeypatch):
    pb = _AuthFakePocketBase(default_unique_fields())
    # users record: plaintext password only inside the test fake
    pb.collection("users").create(
        {
            "id": "u1",
            "email": "owner@x.com",
            "password": "s3cret",
            "role": "member",
            "displayName": "مالک",
        }
    )
    pb.collection("users").create(
        {
            "id": "u2",
            "email": "admin@x.com",
            "password": "adm1n",
            "role": "admin",
            "displayName": "ادمین",
        }
    )
    monkeypatch.setattr("app.middleware.get_pb", lambda: pb)
    return pb


def test_anonymous_redirected_to_login(fake_pb):
    with TestClient(app) as client:
        resp = client.get("/projects", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_anonymous_cannot_reach_dashboard(fake_pb):
    with TestClient(app) as client:
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_public_pages_reachable_anonymously(fake_pb):
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/login").status_code == 200
        assert client.get("/manifest.json").status_code == 200


def test_login_success_sets_session_cookie(fake_pb):
    with TestClient(app) as client:
        resp = client.post("/login", data={"email": "owner@x.com", "password": "s3cret"})
        assert resp.status_code == 200
        cookie = resp.headers.get("set-cookie", "")
        assert "pb_auth=tok-u1" in cookie
        assert "HttpOnly" in cookie
        # now authenticated: /projects no longer redirects
        assert client.get("/projects").status_code == 200


def test_login_wrong_password_rejected(fake_pb):
    with TestClient(app) as client:
        resp = client.post("/login", data={"email": "owner@x.com", "password": "nope"})
        assert resp.status_code == 200
        assert "pb_auth" not in resp.headers.get("set-cookie", "")
        # still anonymous
        assert client.get("/projects", follow_redirects=False).status_code == 303


def test_login_unknown_email_rejected(fake_pb):
    with TestClient(app) as client:
        resp = client.post("/login", data={"email": "ghost@x.com", "password": "x"})
        assert "pb_auth" not in resp.headers.get("set-cookie", "")


def test_htmx_login_error_toast(fake_pb):
    with TestClient(app) as client:
        resp = client.post(
            "/login",
            data={"email": "owner@x.com", "password": "bad"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        import json

        events = json.loads(resp.headers.get("hx-trigger", "{}"))
        assert "اشتباه است" in events["show-toast"]["message"]


def test_logout_clears_cookie(fake_pb):
    with TestClient(app) as client:
        client.post("/login", data={"email": "owner@x.com", "password": "s3cret"})
        assert client.get("/projects").status_code == 200
        resp = client.post("/logout")
        assert resp.status_code == 200
        assert "Max-Age=0" in resp.headers.get("set-cookie", "")
        assert "pb_auth" in resp.headers.get("set-cookie", "")
        # anonymous again
        assert client.get("/projects", follow_redirects=False).status_code == 303


def test_expired_invalid_token_redirects(fake_pb):
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "bogus-token")
        # auth_refresh fails → middleware clears → redirect to login
        resp = client.get("/projects", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_admin_role_passes_through_middleware(fake_pb):
    with TestClient(app) as client:
        client.post("/login", data={"email": "admin@x.com", "password": "adm1n"})
        assert client.get("/projects").status_code == 200
