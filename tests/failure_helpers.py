"""Shared helpers for failure-engineering tests."""

from typing import Any

from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> Any:
    from app.jobs.context import JobContext, ProviderStack
    from app.repositories.jobs import JobEventRepo
    from app.services.settings import ProjectConfig

    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: pb.collection("jobs").update(
            jid, {"progress": pct, **kw}
        ),
        request_cancel=lambda jid: bool(
            (pb.collection("jobs").get_one(jid) or {}).get("cancelRequested")
        ),
    )
