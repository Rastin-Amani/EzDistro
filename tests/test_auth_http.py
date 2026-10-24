"""HTTP-level auth tests via TestClient (real middleware + cookies).

Sign-in is OAuth-only (Google / GitHub) through PocketBase's manual code
exchange. The fake implements the three calls the app makes:
`/auth-methods`, `auth-with-oauth2`, and `auth_refresh` (middleware).
"""

from __future__ import annotations

import json
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
    """Duck-typed users collection: OAuth2 code exchange against stored records."""

    def __init__(self, storage, auth_store: _FakeAuthStore) -> None:
        self._storage = storage
        self._auth = auth_store

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        from tests.fakes import FakeRecordService

        return FakeRecordService(self._storage, "users").create(data)

    def auth_with_oauth2(
        self,
        provider: str,
        code: str,
        code_verifier: str,
        redirect_url: str,
        create_data: dict[str, Any] | None = None,
    ):
        if code != "good-code":
            raise ClientResponseError("invalid code", status=400)
        if (provider, code_verifier) not in {
            ("google", "verifier-google"),
            ("github", "verifier-github"),
        }:
            raise ClientResponseError("bad provider state", status=400)
        for record in self._storage.records("users"):
            if record.get("id") == "u1":
                self._auth.token = "tok-u1"
                self._auth.model = dict(record)
                return SimpleNamespace(token="tok-u1", record=record, meta={"isNew": False})
        raise ClientResponseError("no user", status=400)

    def auth_response(self, response_data: dict[str, Any]):
        token = response_data.pop("token", "")
        record = response_data.pop("record", {})
        if token and record:
            self._auth.save(token, record)
        return SimpleNamespace(token=token, record=record, meta=response_data.get("meta") or {})

    def auth_refresh(self) -> dict[str, Any]:
        token = self._auth.token
        if token.startswith("tok-"):
            user_id = token[4:]
            for record in self._storage.records("users"):
                if record.get("id") == user_id:
                    self._auth.model = dict(record)
                    return dict(record)
        raise ClientResponseError("unauthorized", status=401)


def _auth_methods() -> dict:
    return {
        "password": {"identityFields": ["email"], "enabled": False},
        "oauth2": {
            "enabled": True,
            "providers": [
                {
                    "name": "google",
                    "displayName": "Google",
                    "state": "st-google",
                    "authUrl": "https://accounts.google.com/o/oauth2/v2/auth?client_id=g&state=st-google&redirect_uri=",
                    "codeVerifier": "verifier-google",
                },
                {
                    "name": "github",
                    "displayName": "GitHub",
                    "state": "st-github",
                    "authUrl": "https://github.com/login/oauth/authorize?client_id=h&state=st-github&redirect_uri=",
                    "codeVerifier": "verifier-github",
                },
            ],
        },
    }


class _AuthFakePocketBase(FakePocketBase):
    def __init__(self, unique_fields=None) -> None:
        super().__init__(unique_fields)
        self.auth_store = _FakeAuthStore()
        self.auth_methods_error: Exception | None = None

    def send(self, path: str, req_config: dict) -> dict:
        if path == "/api/collections/users/auth-with-oauth2":
            body = req_config.get("body") or {}
            result = self.collection("users").auth_with_oauth2(
                body.get("provider", ""),
                body.get("code", ""),
                body.get("codeVerifier", ""),
                body.get("redirectURL", ""),
                body.get("createData"),
            )
            return {"token": result.token, "record": dict(result.record), "meta": result.meta}
        if path != "/api/collections/users/auth-methods":
            raise ClientResponseError("not found", status=404)
        if self.auth_methods_error:
            raise self.auth_methods_error
        return _auth_methods()

    def collection(self, name: str):
        if name == "users":
            return _AuthUsersService(self.storage, self.auth_store)
        return super().collection(name)


@pytest.fixture()
def fake_pb(monkeypatch):
    pb = _AuthFakePocketBase(default_unique_fields())
    pb.collection("users").create(
        {
            "id": "u1",
            "email": "owner@x.com",
            "role": "member",
            "displayName": "owner",
        }
    )
    pb.collection("users").create(
        {
            "id": "u2",
            "email": "admin@x.com",
            "role": "admin",
            "displayName": "admin",
        }
    )
    monkeypatch.setattr("app.middleware.get_pb", lambda: pb)
    monkeypatch.setattr("app.middleware.get_data_pb", lambda: pb)
    monkeypatch.setattr("app.api.auth.get_pb", lambda: pb)
    return pb


def _login_state(client: TestClient, provider: str = "google") -> str:
    """Render /login and return the provider state embedded in its href."""
    page = client.get("/login")
    assert page.status_code == 200
    state = "st-" + provider
    assert f"state={state}" in page.text
    return state


def _set_cookies(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _has_session_cookie(response) -> bool:
    """True when the response starts a real session (not just a deletion)."""
    return any(
        cookie.startswith("pb_auth=") and "Max-Age=0" not in cookie
        for cookie in _set_cookies(response)
    )


# ---------------------------------------------------------------------------
# Gating / public pages
# ---------------------------------------------------------------------------
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
        assert client.get("/health").status_code in (200, 503)
        assert client.get("/login").status_code == 200
        assert client.get("/signup").status_code == 200
        assert client.get("/manifest.json").status_code == 200


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------
def test_login_page_offers_only_google_and_github(fake_pb):
    with TestClient(app, base_url="https://app.test") as client:
        resp = client.get("/login")
    assert resp.status_code == 200
    assert 'href="/signup"' in resp.text
    assert "Continue with Google" in resp.text
    assert "Continue with GitHub" in resp.text
    # No password UI survives.
    assert 'type="password"' not in resp.text
    assert "<form" not in resp.text
    assert "Password sign-in" not in resp.text
    # Provider URLs point at our callback, never PocketBase's dashboard.
    assert "redirect_uri=https%3A%2F%2Fapp.test%2Fauth%2Foauth2%2Fcallback" in resp.text
    assert "no-store" in resp.headers.get("cache-control", "")
    cookies = " ".join(_set_cookies(resp))
    assert "oauth_state=" in cookies and "HttpOnly" in cookies and "Path=/auth" in cookies


def test_signup_page_uses_signup_copy(fake_pb):
    with TestClient(app) as client:
        resp = client.get("/signup")
    assert resp.status_code == 200
    assert "Create your account" in resp.text
    assert "Continue with Google" in resp.text
    assert 'href="/login"' in resp.text


def test_login_page_hides_unconfigured_providers(fake_pb):
    fake_pb.send = lambda path, config: {
        "oauth2": {"enabled": True, "providers": [_auth_methods()["oauth2"]["providers"][0]]}
    }
    with TestClient(app) as client:
        resp = client.get("/login")
    assert "Continue with Google" in resp.text
    assert "Continue with GitHub" not in resp.text


def test_login_page_without_providers_explains_the_problem(fake_pb):
    fake_pb.send = lambda path, config: {"oauth2": {"enabled": False, "providers": []}}
    with TestClient(app) as client:
        resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign-in is not configured yet" in resp.text
    assert "oauth_state=" not in " ".join(_set_cookies(resp))


def test_login_page_survives_pocketbase_outage(fake_pb):
    fake_pb.auth_methods_error = ClientResponseError("connection refused", status=0)
    with TestClient(app) as client:
        resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign-in is temporarily unavailable" in resp.text
    assert "Try again" in resp.text


def test_password_login_post_is_gone(fake_pb):
    with TestClient(app) as client:
        resp = client.post(
            "/login",
            data={"email": "owner@x.com", "password": "s3cret"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?error=password"
        assert not _has_session_cookie(resp)
        assert client.get("/projects", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------
def test_oauth_callback_sets_session_cookie(fake_pb):
    with TestClient(app) as client:
        state = _login_state(client)
        resp = client.get(
            f"/auth/oauth2/callback?code=good-code&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard"
        cookies = _set_cookies(resp)
        assert any("pb_auth=tok-u1" in cookie for cookie in cookies)
        assert all("HttpOnly" in cookie for cookie in cookies if "pb_auth=" in cookie)
        assert any(
            cookie.startswith("oauth_state=") and "Max-Age=0" in cookie for cookie in cookies
        )
        # The session is now valid for the middleware.
        assert client.get("/projects").status_code == 200
        assert client.get("/dashboard").status_code == 200


def test_oauth_callback_works_for_github(fake_pb):
    with TestClient(app) as client:
        state = _login_state(client, provider="github")
        resp = client.get(
            f"/auth/oauth2/callback?code=good-code&state={state}",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert any("pb_auth=tok-u1" in cookie for cookie in _set_cookies(resp))


def test_oauth_callback_rejects_a_forged_state(fake_pb):
    with TestClient(app) as client:
        client.get("/login")
        resp = client.get(
            "/auth/oauth2/callback?code=good-code&state=not-from-the-cookie",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?error=state"
        assert not _has_session_cookie(resp)
        assert client.get("/projects", follow_redirects=False).status_code == 303


def test_oauth_callback_without_state_cookie_rejected(fake_pb):
    with TestClient(app) as client:
        resp = client.get(
            "/auth/oauth2/callback?code=good-code&state=st-google",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/login?error=state"


def test_oauth_callback_handles_non_ascii_state(fake_pb):
    """A crafted state must be rejected, not crash the comparison."""
    with TestClient(app) as client:
        client.get("/login")
        resp = client.get(
            "/auth/oauth2/callback?code=good-code&state=%C3%A9",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=state"


def test_oauth_callback_rejects_tampered_state_cookie(fake_pb):
    with TestClient(app) as client:
        client.get("/login")
        cookie = client.cookies.get("oauth_state")
        assert cookie
        client.cookies.set("oauth_state", cookie[:-2] + "aa", path="/auth")
        resp = client.get(
            "/auth/oauth2/callback?code=good-code&state=st-google",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/login?error=state"
    assert not _has_session_cookie(resp)


def test_oauth_callback_cancelled(fake_pb):
    with TestClient(app) as client:
        resp = client.get(
            "/auth/oauth2/callback?error=access_denied&state=st-google",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=cancelled"


def test_oauth_callback_rejected_code(fake_pb):
    with TestClient(app) as client:
        state = _login_state(client)
        resp = client.get(
            f"/auth/oauth2/callback?code=bad-code&state={state}",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/login?error=failed"
    assert not _has_session_cookie(resp)


def test_oauth_callback_pocketbase_unavailable(fake_pb, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise ClientResponseError("connection refused", status=0)

    monkeypatch.setattr(_AuthUsersService, "auth_with_oauth2", unavailable)
    with TestClient(app) as client:
        state = _login_state(client)
        resp = client.get(
            f"/auth/oauth2/callback?code=good-code&state={state}",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=unavailable"


def test_oauth_callback_refuses_disabled_account(fake_pb):
    fake_pb.storage.records("users")[0]["disabled"] = True
    with TestClient(app) as client:
        state = _login_state(client)
        resp = client.get(
            f"/auth/oauth2/callback?code=good-code&state={state}",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/login?error=disabled"
    assert not _has_session_cookie(resp)
    assert any(
        cookie.startswith("pb_auth=") and "Max-Age=0" in cookie for cookie in _set_cookies(resp)
    )


def test_oauth_error_is_shown_on_the_login_page(fake_pb):
    with TestClient(app) as client:
        resp = client.get("/login?error=state")
    assert resp.status_code == 200
    assert 'role="status"' in resp.text
    assert "sign-in link expired" in resp.text


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
def test_logout_clears_cookie(fake_pb):
    with TestClient(app) as client:
        state = _login_state(client)
        client.get(f"/auth/oauth2/callback?code=good-code&state={state}")
        assert client.get("/projects").status_code == 200
        resp = client.post("/logout", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        events = json.loads(resp.headers.get("hx-trigger", "{}"))
        assert "Signed out" in events["show-toast"]["message"]
        assert any(
            cookie.startswith("pb_auth=") and "Max-Age=0" in cookie for cookie in _set_cookies(resp)
        )
        assert client.get("/projects", follow_redirects=False).status_code == 303


def test_expired_invalid_token_redirects(fake_pb):
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "bogus-token")
        resp = client.get("/projects", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_session_cache_avoids_repeat_auth_refresh(fake_pb, monkeypatch):
    """Repeated requests from one token validate against PocketBase only once
    (within the TTL) — the per-request auth_refresh round trip was the main
    request-latency floor."""
    from app.middleware import clear_session_cache

    clear_session_cache()
    calls = {"n": 0}
    original = _AuthUsersService.auth_refresh

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(_AuthUsersService, "auth_refresh", counting)
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "tok-u1")
        assert client.get("/projects").status_code == 200
        assert client.get("/projects").status_code == 200
        assert client.get("/dashboard").status_code == 200
    assert calls["n"] == 1


def test_static_assets_skip_session_validation(fake_pb, monkeypatch):
    """Assets/probes must not pay a PocketBase auth round trip."""
    from app.middleware import clear_session_cache

    clear_session_cache()
    calls = {"n": 0}
    original = _AuthUsersService.auth_refresh

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(_AuthUsersService, "auth_refresh", counting)
    with TestClient(app) as client:
        client.cookies.set("pb_auth", "tok-u1")
        client.get("/static/app.css")
        client.get("/manifest.json")
    assert calls["n"] == 0
