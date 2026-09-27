"""Content workspace tests: outline editing (move/add/delete/brief), status
transitions incl. approved, section generation metadata, and workspace
stat aggregation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.outline_editor import OutlineEditor
from app.services.stats import global_stats
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "\u067e",
            "slug": "p1",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def make_article(pb: FakePocketBase, project_id: str, status: str = "review") -> dict[str, Any]:
    topic = TopicRepo(pb).create(project=project_id, title="\u062a", keyword="\u06a9")
    article = pb.collection("articles").create(
        {
            "project": project_id,
            "topicId": topic["id"],
            "title": "\u0639\u0646\u0648\u0627\u0646",
            "slug": "onvan",
            "status": status,
            "outlineVersion": 1,
            "outline": {
                "title": "\u0639\u0646\u0648\u0627\u0646",
                "slug": "onvan",
                "sections": [
                    {
                        "heading": "\u0645\u0642\u062f\u0645\u0647",
                        "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0645\u0642\u062f\u0645\u0647",
                        "internal_links": [],
                    },
                    {
                        "heading": "\u0628\u062f\u0646\u0647",
                        "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0628\u062f\u0646\u0647",
                        "internal_links": [],
                    },
                    {
                        "heading": "\u0646\u062a\u06cc\u062c\u0647",
                        "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0646\u062a\u06cc\u062c\u0647",
                        "internal_links": [],
                    },
                ],
            },
        }
    )
    for i, plan in enumerate(article["outline"]["sections"]):
        pb.collection("article_sections").create(
            {
                "article": article["id"],
                "position": i,
                "heading": plan["heading"],
                "contentBrief": plan["content_brief"],
                "internalLinks": [],
                "status": "done",
                "content": f"<h2>{plan['heading']}</h2><p>\u0645\u062d\u062a\u0648\u0627 {i}</p>",
                "generationAttempts": 1,
                "promptVersion": 1,
                "provider": "openai_compat",
                "model": "gpt-4o-mini",
                "generationLatency": 100,
                "tokenUsage": {"completion_tokens": 50},
            }
        )
    return pb.collection("articles").get_one(article["id"])


# ---------------------------------------------------------------------------
# Outline editing — versioned, deterministic
# ---------------------------------------------------------------------------
def test_outline_move_keeps_content_and_bumps_version():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    editor = OutlineEditor(pb)

    updated = editor.move(article, 0, "down")  # \u0645\u0642\u062f\u0645\u0647 → position 1
    assert updated["outlineVersion"] == 2
    assert [s["heading"] for s in updated["outline"]["sections"]] == [
        "\u0628\u062f\u0646\u0647",
        "\u0645\u0642\u062f\u0645\u0647",
        "\u0646\u062a\u06cc\u062c\u0647",
    ]

    rows = SectionRepo(pb).list_for_article(article["id"])
    assert [r["heading"] for r in rows] == [
        "\u0628\u062f\u0646\u0647",
        "\u0645\u0642\u062f\u0645\u0647",
        "\u0646\u062a\u06cc\u062c\u0647",
    ]
    # content preserved on pure moves
    assert all(r["status"] == "done" for r in rows)
    assert rows[1]["content"]  # \u0645\u0642\u062f\u0645\u0647 kept its content


def test_outline_add_appends_pending_section():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    editor = OutlineEditor(pb)

    updated = editor.add(
        article,
        "\u0633\u0648\u0627\u0644\u0627\u062a \u0645\u062a\u062f\u0627\u0648\u0644",
        "\u067e\u0627\u0633\u062e \u0628\u0647 \u0633\u0648\u0627\u0644\u0627\u062a",
    )
    assert updated["outlineVersion"] == 2
    assert (
        updated["outline"]["sections"][-1]["heading"]
        == "\u0633\u0648\u0627\u0644\u0627\u062a \u0645\u062a\u062f\u0627\u0648\u0644"
    )
    rows = SectionRepo(pb).list_for_article(article["id"])
    assert len(rows) == 4
    assert rows[-1]["status"] == "pending"
    assert (
        rows[-1]["contentBrief"]
        == "\u067e\u0627\u0633\u062e \u0628\u0647 \u0633\u0648\u0627\u0644\u0627\u062a"
    )


def test_outline_delete_removes_section_and_renumbers():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    editor = OutlineEditor(pb)

    updated = editor.delete(article, 1)  # \u062d\u0630\u0641 «\u0628\u062f\u0646\u0647»
    assert updated["outlineVersion"] == 2
    assert [s["heading"] for s in updated["outline"]["sections"]] == [
        "\u0645\u0642\u062f\u0645\u0647",
        "\u0646\u062a\u06cc\u062c\u0647",
    ]
    rows = SectionRepo(pb).list_for_article(article["id"])
    assert len(rows) == 2
    assert [r["position"] for r in rows] == [0, 1]  # renumbered


def test_outline_brief_edit_resets_section_for_regeneration():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    editor = OutlineEditor(pb)

    updated = editor.update_brief(
        article,
        0,
        "\u0645\u0642\u062f\u0645\u0647",
        "\u062e\u0644\u0627\u0635\u0647 \u062c\u062f\u06cc\u062f \u0645\u0642\u062f\u0645\u0647",
    )
    assert updated["outlineVersion"] == 2
    rows = SectionRepo(pb).list_for_article(article["id"])
    assert (
        rows[0]["contentBrief"]
        == "\u062e\u0644\u0627\u0635\u0647 \u062c\u062f\u06cc\u062f \u0645\u0642\u062f\u0645\u0647"
    )
    assert rows[0]["status"] == "pending"  # must regenerate
    assert rows[0]["content"] == ""
    assert rows[1]["status"] == "done"  # other sections untouched
    assert rows[1]["content"]


def test_outline_ops_validate_positions():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    editor = OutlineEditor(pb)
    with pytest.raises(ValueError):
        editor.move(article, 0, "up")
    with pytest.raises(ValueError):
        editor.delete(article, 9)
    with pytest.raises(ValueError):
        editor.update_brief(article, 5, "x", "y")
    with pytest.raises(ValueError):
        editor.add(article, "   ", "\u062e\u0644\u0627\u0635\u0647")


# ---------------------------------------------------------------------------
# Status transitions incl. approved
# ---------------------------------------------------------------------------
def test_article_status_transitions():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"], status="review")
    repo = ArticleRepo(pb)

    repo.set_status(article["id"], "approved")
    assert repo.get(article["id"])["status"] == "approved"
    repo.set_status(article["id"], "publishing")
    repo.mark_published(article["id"], 42, "https://site.test/?p=42")
    assert repo.get(article["id"])["status"] == "published"


# ---------------------------------------------------------------------------
# Section generation metadata + started-at
# ---------------------------------------------------------------------------
def test_mark_generating_records_started_at():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    section = SectionRepo(pb).list_for_article(article["id"])[0]
    SectionRepo(pb).set_status(section["id"], "pending")
    SectionRepo(pb).mark_generating(section["id"])
    updated = SectionRepo(pb).get(section["id"])
    assert updated["status"] == "generating"
    assert updated["generationStartedAt"]  # live elapsed computed from this
    assert updated["generationAttempts"] == 2


# ---------------------------------------------------------------------------
# Workspace stats aggregation
# ---------------------------------------------------------------------------
def test_workspace_stats_aggregates_pipeline():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    pb.collection("topics").create(
        {"project": project["id"], "title": "\u062a\u06f1", "status": "planned"}
    )
    pb.collection("topics").create(
        {"project": project["id"], "title": "\u062a\u06f2", "status": "failed"}
    )
    for status in ("generating", "approved", "published"):
        topic = TopicRepo(pb).create(
            project=project["id"], title=f"\u062a {status}", keyword="\u06a9"
        )
        TopicRepo(pb).set_status(
            topic["id"],
            "writing"
            if status == "generating"
            else "review"
            if status == "approved"
            else "published",
        )
        pb.collection("articles").create(
            {
                "project": project["id"],
                "topicId": topic["id"],
                "title": status,
                "slug": status,
                "status": status,
                "outlineVersion": 1,
            }
        )
    pb.collection("index_runs").create({"project": project["id"], "status": "succeeded"})
    pb.collection("integrations").create(
        {
            "project": project["id"],
            "category": "llm",
            "provider": "g",
            "displayName": "g",
            "enabled": True,
            "healthStatus": "healthy",
        }
    )

    stats = global_stats(pb)
    assert stats["topics_planned"] == 1
    assert stats["articles_writing"] == 1  # generating
    assert stats["articles_review"] == 1  # approved
    assert stats["articles_published"] == 1
    assert stats["indexing_ok"] == 1
    assert stats["indexing_failed"] == 0
    assert stats["provider_healthy"] == 1
    assert stats["provider_unhealthy"] == 0


def test_workspace_stats_indexing_health_flags_failures():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    pb.collection("index_runs").create({"project": project["id"], "status": "failed"})
    stats = global_stats(pb)
    assert stats["indexing_ok"] == 0
    assert stats["indexing_failed"] == 1


def test_workspace_stats_empty_scope_does_not_crash():
    """Members with no project memberships (scope=[]) get zeroed stats — not a
    malformed PocketBase filter (regression: a leading '&&' → 400 → /dashboard 500)."""
    from app.services.stats import recent_events, recent_jobs

    pb = FakePocketBase(default_unique_fields())
    make_project(pb)  # data exists but this user cannot see it
    stats = global_stats(pb, [])
    assert stats["projects"] == 0
    assert stats["jobs_pending"] == 0
    assert stats["jobs_completed"] == 0
    assert stats["average_duration_s"] == 0.0
    assert stats["failure_rate"] == 0.0
    assert stats["indexing_ok"] == 0
    assert stats["provider_healthy"] == 0
    assert recent_jobs(pb, []) == []
    assert recent_events(pb, []) == []


def test_workspace_template_renders_section_statuses():
    """Regression: _section_status.html expects `section`, but the workspace
    loop used `s` — any workspace page with sections raised UndefinedError."""
    from app.templates import templates

    class FakeRequest:
        url = type("U", (), {"path": "/projects/p1/articles/a1/workspace"})()

        def url_for(self, name: str, **path_params: Any) -> str:
            return "/"

    context = {
        "request": FakeRequest(),
        "title": "\u062a\u0633\u062a",
        "project": {"id": "p1", "slug": "p1", "name": "\u067e\u0631\u0648\u0698\u0647"},
        "article": {
            "id": "a1",
            "title": "\u0645\u0642\u0627\u0644\u0647",
            "status": "review",
            "wordCount": 888,
            "outlineVersion": 1,
            "validation": {"ok": True, "issues": []},
        },
        "sections": [
            {
                "id": "s1",
                "heading": "\u0645\u0642\u062f\u0645\u0647",
                "status": "done",
                "generationLatency": 120,
                "model": "deepseek-v3.2",
                "contentBrief": "",
                "content": "<p>x</p>",
                "keyword": "",
                "promptVersion": 1,
                "provider": "openai_compat",
            },
            {
                "id": "s2",
                "heading": "\u0628\u062f\u0646\u0647",
                "status": "generating",
                "generationLatency": 0,
                "model": "deepseek-v3.2",
                "contentBrief": "",
                "content": "",
                "keyword": "",
                "promptVersion": 1,
                "provider": "openai_compat",
            },
            {
                "id": "s3",
                "heading": "\u0646\u062a\u06cc\u062c\u0647",
                "status": "failed",
                "generationLatency": 0,
                "model": "",
                "contentBrief": "",
                "content": "",
                "keyword": "",
                "promptVersion": 0,
                "provider": "",
                "error": {"message": "too short", "type": "SectionValidationError"},
            },
        ],
        "topic": {"title": "\u0645\u0648\u0636\u0648\u0639", "keyword": "\u0633\u0626\u0648"},
        "publish_runs": [],
        "internal_links": [],
    }
    html = templates.get_template("pages/articles/workspace.html").render(context)
    assert "Generating" in html  # generating badge
    assert "Failed" in html  # failed badge
    assert "Done" in html  # done badge
