"""Shared builders for route-level tests (direct invocation with FakePocketBase).

Kept small and composable: each builder creates one valid baseline record;
tests override only the fields relevant to the scenario.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.i18n import get_locale, set_request_locale
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def make_user(uid: str = "u1", role: str = "member") -> dict:
    return {"id": uid, "role": role, "email": f"{uid}@x.com"}


def make_req(pb: FakePocketBase, user: dict, project_id: str) -> SimpleNamespace:
    def url_for(name: str, **path_params):
        return "/" + name

    return SimpleNamespace(
        state=SimpleNamespace(pb=pb, user=user, req_id="r1"),
        headers={"HX-Request": "true"},
        url=SimpleNamespace(path=f"/projects/{project_id}"),
        query_params={},
        url_for=url_for,
    )


def make_req_plain(pb: FakePocketBase, user: dict, project_id: str = "") -> SimpleNamespace:
    """Request without HX-Request (for require_hx rejection tests)."""
    req = make_req(pb, user, project_id)
    req.headers = {}
    return req


def call_route(fn, request, *args, **kwargs):
    """Invoke a route in Persian for legacy source-language assertions."""
    import inspect

    from fastapi.params import Form

    previous_locale = get_locale().code
    set_request_locale("fa")
    try:
        for name, param in inspect.signature(fn).parameters.items():
            if name in kwargs or name == "request":
                continue
            if isinstance(param.default, Form):
                kwargs[name] = ""
        response = fn(request, *args, **kwargs)
        getattr(response, "body", None)  # render lazy template responses in this locale
        return response
    finally:
        set_request_locale(previous_locale)


async def call_route_async(fn, request, *args, **kwargs):
    """Async counterpart for direct route tests that assert Persian copy."""
    previous_locale = get_locale().code
    set_request_locale("fa")
    try:
        response = await call_route(fn, request, *args, **kwargs)
        getattr(response, "body", None)
        return response
    finally:
        set_request_locale(previous_locale)


def toast_message(resp) -> str:
    """Extract the toast text from a mutation response's HX-Trigger header."""
    events = json.loads(resp.headers.get("HX-Trigger", "{}"))
    return events.get("show-toast", {}).get("message", "")


def hx_events(resp) -> dict[str, Any]:
    return json.loads(resp.headers.get("HX-Trigger", "{}"))


def make_project(pb: FakePocketBase, slug: str = "proj-a", name: str = "A") -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": name,
            "slug": slug,
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
            "description": "",
            "createdBy": "",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def make_member(
    pb: FakePocketBase, project_id: str, user_id: str = "u1", role: str = "owner"
) -> dict[str, Any]:
    return pb.collection("project_members").create(
        {"project": project_id, "user": user_id, "role": role}
    )


def make_topic(
    pb: FakePocketBase,
    project_id: str,
    *,
    title: str = "Topic",
    keyword: str = "",
    status: str = "planned",
    priority: int = 0,
) -> dict[str, Any]:
    topic = TopicRepo(pb).create(
        project=project_id, title=title, keyword=keyword, priority=priority
    )
    if status != "planned":
        TopicRepo(pb).set_status(topic["id"], status)
    return TopicRepo(pb).get(topic["id"])


def make_article(
    pb: FakePocketBase,
    project_id: str,
    *,
    status: str = "review",
    final_html: str | None = None,
    wp_id: int | None = None,
    topic_id: str = "",
) -> dict[str, Any]:
    topic = topic_id or make_topic(pb, project_id, status="published")["id"]
    data: dict[str, Any] = {
        "project": project_id,
        "topicId": topic,
        "title": "Article title",
        "slug": "onvan",
        "status": status,
        "outlineVersion": 1,
        "metaDescription": "\u0645",
        "outline": {
            "title": "Article title",
            "slug": "onvan",
            "sections": [
                {
                    "heading": "\u0645\u0642\u062f\u0645\u0647",
                    "content_brief": "\u062e\u0644\u0627\u0635\u0647",
                    "internal_links": [],
                },
                {
                    "heading": "\u0628\u062f\u0646\u0647",
                    "content_brief": "\u062e\u0644\u0627\u0635\u0647",
                    "internal_links": [],
                },
            ],
        },
    }
    if final_html is not None:
        data["finalHtml"] = final_html
    if wp_id:
        data["wordpressPostId"] = wp_id
        data["wordpressUrl"] = f"https://s.test/?p={wp_id}"
    article = pb.collection("articles").create(data)
    body_words = " ".join(f"\u06a9\u0644\u0645\u0647 {i}" for i in range(60))
    for i, plan in enumerate(data["outline"]["sections"]):
        pb.collection("article_sections").create(
            {
                "article": article["id"],
                "position": i,
                "heading": plan["heading"],
                "contentBrief": plan["content_brief"],
                "internalLinks": [],
                "status": "done",
                "content": f"<h2>{plan['heading']}</h2><p>{body_words}</p>",
                "generationAttempts": 1,
                "promptVersion": 1,
                "provider": "openai_compat",
                "model": "gpt-4o-mini",
            }
        )
    return pb.collection("articles").get_one(article["id"])


def make_section(pb: FakePocketBase, article_id: str, *, position: int = 0) -> dict[str, Any]:
    return pb.collection("article_sections").create(
        {
            "article": article_id,
            "position": position,
            "heading": "\u0628\u062e\u0634",
            "contentBrief": "\u062e\u0644\u0627\u0635\u0647",
            "internalLinks": [],
            "status": "pending",
            "content": "",
        }
    )


def make_job(
    pb: FakePocketBase,
    project_id: str,
    *,
    job_type: str = "write_article",
    status: str = "pending",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.repositories.jobs import now_utc, pb_dt

    return pb.collection("jobs").create(
        {
            "project": project_id,
            "type": job_type,
            "payload": payload or {},
            "idempotencyKey": f"{job_type}:{project_id}:{abs(hash(str(payload)))}:{status}",
            "status": status,
            "attempts": 0,
            "maxAttempts": 3,
            "progress": 0,
            "cancelRequested": False,
            "availableAt": pb_dt(now_utc()),
        }
    )


def make_job_event(
    pb: FakePocketBase, job_id: str, *, event_type: str = "job.started", message: str = ""
) -> dict[str, Any]:
    return pb.collection("job_events").create(
        {
            "job": job_id,
            "eventType": event_type,
            "message": message or event_type,
            "metadata": {},
        }
    )


def make_document(pb: FakePocketBase, project_id: str) -> dict[str, Any]:
    return pb.collection("documents").create(
        {
            "project": project_id,
            "sourceType": "wordpress",
            "sourceId": "100",
            "title": "\u0645\u0633\u062a\u0646\u062f",
            "sourceUrl": "https://s.test/?p=100",
            "contentHash": "abc",
            "indexStatus": "indexed",
            "chunkCount": 2,
            "embeddingModel": "text-embedding-3-small",
            "embeddingDimensions": 1536,
        }
    )


def make_index_run(
    pb: FakePocketBase, project_id: str, *, status: str = "failed"
) -> dict[str, Any]:
    return pb.collection("index_runs").create(
        {
            "project": project_id,
            "job": "job-x",
            "trigger": "manual",
            "status": status,
            "lastSourceId": "50",
            "error": {},
        }
    )


def make_publish_run(
    pb: FakePocketBase, article_id: str, project_id: str, *, status: str = "failed"
) -> dict[str, Any]:
    return pb.collection("publishing_runs").create(
        {
            "project": project_id,
            "article": article_id,
            "job": "job-p",
            "mode": "publish",
            "status": status,
            "requestId": "req1",
            "attempt": 1,
            "error": {"type": "ProviderError", "message": "wp down"},
        }
    )


def make_prompt(pb: FakePocketBase, project_id: str, ptype: str = "outline_user") -> dict[str, Any]:
    return PromptRepo(pb).save_version(
        project_id=project_id,
        ptype=ptype,
        name="default",
        content="JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646.",
    )


def make_schedule(pb: FakePocketBase, project_id: str, *, kind: str = "index") -> dict[str, Any]:
    from app.repositories.schedules import ScheduleRepo

    return ScheduleRepo(pb).create(project=project_id, name=kind, kind=kind, interval_minutes=1440)


def make_integration(pb: FakePocketBase, project_id: str) -> dict[str, Any]:
    return pb.collection("integrations").create(
        {
            "project": project_id,
            "category": "llm",
            "provider": "openai_compat",
            "displayName": "LLM \u0627\u0635\u0644\u06cc",
            "configuration": {},
            "secretsEnc": "",
            "enabled": True,
        }
    )


def make_project_membership(pb: FakePocketBase, project_id: str, user_id: str = "u1") -> dict:
    return make_member(pb, project_id, user_id)
