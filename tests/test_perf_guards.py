"""Structural performance guards.

These assert *invariants*, never wall-clock timings — a slow CI machine must not
turn them red, but accidentally reintroducing the regression must.
"""

from __future__ import annotations

import subprocess
import sys


def test_registry_import_does_not_load_qdrant_client():
    """`qdrant_client` costs seconds to import (numpy + grpc + generated models).

    The provider registry imports the vector adapter eagerly, so a module-level
    `from qdrant_client import ...` in qdrant_store.py put that cost on the first
    request that resolved any provider (e.g. project settings). A fresh
    subprocess is the only reliable way to check the import graph.
    """
    code = (
        "import sys;"
        "import app.providers.registry;"  # noqa: E702
        "import app.providers.vector.qdrant_store;"
        "sys.exit(1 if 'qdrant_client' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_pocketbase_clients_share_one_transport():
    """One warm, bounded connection pool for the whole process.

    Per-client pools meant an idle-then-navigate request paid a fresh TCP+TLS
    handshake for every query (and abandoned sockets piled up).
    """
    from app.pb import _TRANSPORT, _client

    assert _client().http_client._transport is _TRANSPORT


def test_asset_version_is_memoized(monkeypatch):
    """base.html asks six times per render; it must not walk the static tree each time."""
    import time

    import app.templates as t

    monkeypatch.setattr(t, "_asset_version_cache", (time.monotonic(), "sentinel"))
    assert t.asset_version() == "sentinel"


def test_batch_chunks_respect_count_and_size():
    from app.pb import batch_chunks

    assert [len(c) for c in batch_chunks(list(range(120)), max_requests=50)] == [50, 50, 20]

    # A size cap must split further, and never drop or duplicate an item.
    big = [{"x": "y" * 400} for _ in range(10)]
    chunks = batch_chunks(big, max_requests=50, max_bytes=1000)
    assert sum(len(c) for c in chunks) == 10
    assert len(chunks) > 1


def test_lease_acquire_returns_id_and_release_uses_it():
    """The winner already knows its lease row id — release() must not re-read it.

    `acquire` returning the id (None on a lost race) is what lets the engine's
    heartbeat and release cost one request instead of two.
    """
    import datetime as dt

    from app.repositories.jobs import LeaseRepo, now_utc
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    leases = LeaseRepo(pb)
    expires = now_utc() + dt.timedelta(seconds=60)

    lease_id = leases.acquire("job-1", "worker-a", expires)
    assert lease_id, "acquire must return the created lease id"
    assert leases.acquire("job-1", "worker-b", expires) is None, "unique constraint must lose"

    leases.release("job-1", lease_id)
    assert leases.current("job-1") is None


def test_batch_send_degrades_instead_of_raising():
    """A server with the batch endpoint disabled must not break bulk writes.

    `create_many`/`delete_matching` fall back to one request per row when this
    returns None, so the batch endpoint stays a pure optimisation.
    """

    class NoBatch:
        base_url = "http://127.0.0.1:1"

        class http_client:
            @staticmethod
            def post(*_args, **_kwargs):
                raise RuntimeError("batch endpoint unavailable")

        class auth_store:
            token = ""

    from app.pb import batch_send

    assert batch_send(NoBatch(), [{"method": "DELETE", "url": "/x"}]) is None
