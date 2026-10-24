"""Bootstrap: OAuth-only sign-in provisioning (users collection auth options)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.scripts import bootstrap_pb


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _collection() -> dict:
    return {
        "oauth2": {
            "enabled": False,
            "mappedFields": {
                "id": "",
                "name": "displayName",
                "username": "",
                "avatarURL": "avatar",
            },
            "providers": [
                {
                    "pkce": None,
                    "name": "google",
                    "clientId": "google-id",
                    "authURL": "",
                    "tokenURL": "",
                    "userInfoURL": "",
                    "displayName": "",
                    "extra": {},
                },
                {"name": "microsoft", "clientId": "ms-id", "clientSecret": "x"},
            ],
        },
        "passwordAuth": {"enabled": True, "identityFields": ["email"]},
        "otp": {"enabled": True, "duration": 180},
        "mfa": {"enabled": False, "duration": 600},
    }


def _pb() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://pb.local", auth_store=SimpleNamespace(token="admin"))


@pytest.fixture()
def patched(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    monkeypatch.setattr(settings, "github_oauth_client_id", "gh-client")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "gh-secret")
    monkeypatch.setattr(bootstrap_pb.httpx, "get", lambda *a, **k: _Response(200, _collection()))

    def patch(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Response(200, {})

    monkeypatch.setattr(bootstrap_pb.httpx, "patch", patch)
    return calls


def test_oauth_only_and_provider_union(patched):
    bootstrap_pb.ensure_users_auth(_pb())
    payload = patched[0]
    assert payload["passwordAuth"]["enabled"] is False
    assert payload["passwordAuth"]["identityFields"] == ["email"]  # preserved
    assert payload["otp"]["enabled"] is False
    assert payload["mfa"]["enabled"] is False
    assert payload["oauth2"]["enabled"] is True
    # Only google/github, ordered by the read list, with GitHub appended.
    assert [p["name"] for p in payload["oauth2"]["providers"]] == ["google", "github"]
    google, github = payload["oauth2"]["providers"]
    # PocketBase merges by provider name; the redacted secret must not be sent
    # back (it would be wiped on a server without the merge).
    assert google["clientId"] == "google-id"
    assert "clientSecret" not in google
    assert github["clientId"] == "gh-client"
    assert github["clientSecret"] == "gh-secret"


def test_old_pocketbase_fallback_still_disables_password(monkeypatch, patched):
    responses = iter(
        [
            _Response(400, text="validation: clientSecret is required"),
            _Response(200, {}),
        ]
    )
    bodies: list[dict] = []

    def patch(url, headers=None, json=None, timeout=None):
        bodies.append(json)
        return next(responses)

    monkeypatch.setattr(bootstrap_pb.httpx, "patch", patch)
    bootstrap_pb.ensure_users_auth(_pb())
    assert "oauth2" in bodies[0]
    assert "oauth2" not in bodies[1]
    assert bodies[1]["passwordAuth"]["enabled"] is False


def test_unrelated_schema_error_is_not_swallowed(monkeypatch, patched):
    monkeypatch.setattr(bootstrap_pb.httpx, "patch", lambda *a, **k: _Response(400, text="boom"))
    with pytest.raises(RuntimeError):
        bootstrap_pb.ensure_users_auth(_pb())
