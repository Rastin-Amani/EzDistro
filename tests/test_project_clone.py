"""Project duplication — a new project can inherit another's configuration.

Copies settings, connections, active prompts and schedules; never content
(topics/articles) or research. The source must be writable by the caller.
"""

from __future__ import annotations

from app.api import projects as P
from app.repositories.integrations import IntegrationRepo
from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.prompts import PromptRepo
from app.repositories.schedules import ScheduleRepo
from tests.helpers import (
    call_route,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_topic,
    make_user,
    toast_message,
)


def _seed_source(pb):
    source = make_project(pb, slug="source", name="Source")
    make_member(pb, source["id"], "u1", "owner")
    ProjectSettingsRepo(pb).upsert(source["id"], {"brandName": "Acme", "targetLocale": "de-DE"})
    IntegrationRepo(pb).create(
        project=source["id"],
        category="llm",
        provider="openai_compat",
        display_name="My LLM",
        model="gpt-x",
        configuration={"base_url": "https://api.example.com", "masked": "sk-…"},
        secrets_enc="ciphertext",
    )
    PromptRepo(pb).save_version(
        project_id=source["id"], ptype="outline_system", name="default", content="Outline v1"
    )
    PromptRepo(pb).save_version(
        project_id=source["id"], ptype="outline_system", name="default", content="Outline v2"
    )
    ScheduleRepo(pb).create(
        project=source["id"], name="write", kind="write", interval_minutes=60, enabled=True
    )
    make_topic(pb, source["id"], title="Kept in source")
    return source


def test_create_project_duplicates_configuration_not_content():
    pb = make_pb()
    source = _seed_source(pb)
    req = make_req(pb, make_user(), "")

    resp = call_route(
        P.create_project, req, name="Clone", slug="clone", source_project_id=source["id"]
    )

    target = ProjectRepo(pb).by_slug("clone")
    assert target is not None
    assert source["id"] in toast_message(resp) or "duplicated" in toast_message(resp)

    # settings (merged over defaults) came along
    assert ProjectSettingsRepo(pb).get_for_project(target["id"])["brandName"] == "Acme"
    assert ProjectSettingsRepo(pb).get_for_project(target["id"])["targetLocale"] == "de-DE"

    # connections, secrets ciphertext included; health reset
    copied = IntegrationRepo(pb).list_for_project(target["id"])
    assert len(copied) == 1
    assert copied[0]["model"] == "gpt-x"
    assert copied[0]["secretsEnc"] == "ciphertext"
    assert copied[0]["healthStatus"] == "unknown"

    # only the ACTIVE prompt version, at version 1 in the new project
    prompts = PromptRepo(pb).list_for_project(target["id"])
    assert [(p["version"], p["content"], p["active"]) for p in prompts] == [(1, "Outline v2", True)]

    # schedules copied, next run scheduled afresh
    schedules = ScheduleRepo(pb).list_for_project(target["id"])
    assert [(s["kind"], s["intervalMinutes"], s["enabled"]) for s in schedules] == [
        ("write", 60, True)
    ]

    # content is NOT copied
    assert len(pb.storage.records("topics")) == 1


def test_create_project_without_source_stays_empty():
    pb = make_pb()
    _seed_source(pb)
    req = make_req(pb, make_user(), "")
    call_route(P.create_project, req, name="Fresh", slug="fresh")
    target = ProjectRepo(pb).by_slug("fresh")
    assert IntegrationRepo(pb).list_for_project(target["id"]) == []
    assert PromptRepo(pb).list_for_project(target["id"]) == []
    assert ProjectSettingsRepo(pb).get_for_project(target["id"])["brandName"] == ""


def test_create_project_refuses_unwritable_source():
    pb = make_pb()
    source = make_project(pb, slug="theirs", name="Theirs")
    make_member(pb, source["id"], "u2", "owner")
    req = make_req(pb, make_user(), "")
    resp = call_route(
        P.create_project, req, name="Steal", slug="steal", source_project_id=source["id"]
    )
    assert "failed" in toast_message(resp)
    assert ProjectRepo(pb).by_slug("steal") is None
