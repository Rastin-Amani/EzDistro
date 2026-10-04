"""created/updated are app-written: the live PocketBase keeps legacy `date`
system fields whose onCreate/onUpdate flags PocketBase silently strips, so
repositories stamp timestamps themselves (explicit writes are accepted)."""

from __future__ import annotations

import re

from app.repositories.projects import ProjectRepo
from tests.helpers import make_pb

PB_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_create_and_update_write_pocketbase_timestamps():
    pb = make_pb()
    repo = ProjectRepo(pb)

    proj = repo.create(name="T", slug="t")
    assert PB_TS.match(proj.get("created") or ""), proj.get("created")
    assert PB_TS.match(proj.get("updated") or ""), proj.get("updated")

    updated = repo.update(proj["id"], {"name": "T2"})
    assert updated["created"] == proj["created"]
    assert PB_TS.match(updated.get("updated") or ""), updated.get("updated")


def test_callers_can_override_timestamps():
    pb = make_pb()
    repo = ProjectRepo(pb)

    proj = repo.create(name="Old", slug="old")
    stamped = repo.update(
        proj["id"],
        {"updated": "2020-01-01 00:00:00.000Z"},
    )
    assert stamped["updated"] == "2020-01-01 00:00:00.000Z"
