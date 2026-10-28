"""A worker's PocketBase superuser token expires (24 h by default).

A worker that authenticates once at startup goes blind at expiry: every request
is a 401, so heartbeats stop (the Workers page says "offline"), no job is
claimed and schedules stall — while the process is still alive. That is exactly
what happened on 2026-10-28 00:08 UTC, 24 h after both workers started.
"""

from __future__ import annotations

import asyncio

import pytest

from app import pb as pb_module
from app.workers import worker as worker_module


class _Auth:
    def __init__(self, calls: list[str], fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def auth_with_password(self, email: str, password: str) -> None:
        self.calls.append(email)
        if self.fail:
            raise RuntimeError("pb down")


class _FakePb:
    """Just enough client for `_authenticate_admin`."""

    def __init__(self, calls: list[str], fail_superusers: bool = False) -> None:
        self.calls = calls
        self.fail_superusers = fail_superusers

    def collection(self, name: str) -> _Auth:
        if name == "_superusers":
            return _Auth(self.calls, fail=self.fail_superusers)
        return _Auth(self.calls)


@pytest.fixture
def admin_creds(monkeypatch):
    monkeypatch.setattr(pb_module.settings, "pb_admin_email", "admin@example.com")
    monkeypatch.setattr(pb_module.settings, "pb_admin_password", "secret")


def test_reauth_reuses_the_same_client(admin_creds):
    calls: list[str] = []
    fake = _FakePb(calls)

    pb_module.reauth_admin(fake)

    assert calls == ["admin@example.com"]


def test_reauth_falls_back_to_legacy_admins(admin_creds):
    calls: list[str] = []
    pb_module.reauth_admin(_FakePb(calls, fail_superusers=True))
    assert calls  # both collections were tried


def test_refresh_loop_keeps_the_token_alive(monkeypatch, admin_creds):
    calls: list[str] = []
    monkeypatch.setattr(worker_module, "ADMIN_REFRESH_INTERVAL", 0.01)
    monkeypatch.setattr(worker_module, "ADMIN_REFRESH_RETRY", 0.01)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker_module.admin_token_refresh(_FakePb(calls), stop))
        await asyncio.sleep(0.05)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_failed_refresh_never_kills_the_loop(monkeypatch, admin_creds):
    calls: list[str] = []
    monkeypatch.setattr(worker_module, "ADMIN_REFRESH_INTERVAL", 0.01)
    monkeypatch.setattr(worker_module, "ADMIN_REFRESH_RETRY", 0.01)

    class _Broken(_FakePb):
        def collection(self, name: str) -> _Auth:
            return _Auth(self.calls, fail=True)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker_module.admin_token_refresh(_Broken(calls), stop))
        await asyncio.sleep(0.05)
        assert not task.done()  # still retrying
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) >= 2
