"""Prompt versioning + project membership tests."""

from __future__ import annotations

from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo
from app.repositories.prompts import PromptRepo
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    pb = FakePocketBase(default_unique_fields())
    pb.collection("users").create(
        {"email": "a@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    return pb


def test_prompt_save_versioning_appends_and_activates():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p1")
    repo = PromptRepo(pb)

    first = repo.save_version(
        project_id=project["id"],
        ptype="seo_rules",
        name="default",
        content="version 1",
    )
    assert first["version"] == 1
    assert first["active"] is True

    second = repo.save_version(
        project_id=project["id"],
        ptype="seo_rules",
        name="default",
        content="version 2",
    )
    assert second["version"] == 2
    assert second["active"] is True

    rows = repo.history(project["id"], "seo_rules")
    assert len(rows) == 2
    assert rows[0]["version"] == 2  # newest first
    assert rows[0]["active"] is True
    assert rows[1]["active"] is False  # old version deactivated, kept as history


def test_prompt_resolution_project_wins_over_global():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p2")
    repo = PromptRepo(pb)

    repo.save_version(
        project_id=None,
        ptype="brand_voice",
        name="default",
        content="Global brand voice",
    )
    repo.save_version(
        project_id=project["id"],
        ptype="brand_voice",
        name="default",
        content="Project brand voice",
    )

    assert repo.resolve(project["id"], "brand_voice") == "Project brand voice"

    # project without a row falls back to the global default
    project2 = ProjectRepo(pb).create(name="P2", slug="p3")
    assert repo.resolve(project2["id"], "brand_voice") == "Global brand voice"

    # unknown type → empty
    assert repo.resolve(project["id"], "validation") == ""


def test_prompt_invalid_type_rejected():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p4")
    import pytest

    with pytest.raises(ValueError):
        PromptRepo(pb).save_version(project_id=project["id"], ptype="nope", name="d", content="x")


def test_membership_unique_and_role_update():
    pb = make_pb()
    project = ProjectRepo(pb).create(name="P", slug="p5")
    user = pb.collection("users").create(
        {"email": "b@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    repo = MemberRepo(pb)

    repo.add(project=project["id"], user=user["id"], role="viewer")
    repo.add(project=project["id"], user=user["id"], role="viewer")  # idempotent
    assert len(pb.collection("project_members").get_full_list()) == 1

    repo.add(project=project["id"], user=user["id"], role="editor")  # role upgrade
    assert repo.role_of(project["id"], user["id"]) == "editor"
    assert len(pb.collection("project_members").get_full_list()) == 1

    members = repo.list_for_user(user["id"])
    assert members[0]["project"] == project["id"]

    repo.remove(project["id"], user["id"])
    assert repo.role_of(project["id"], user["id"]) == ""
