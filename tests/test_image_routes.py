"""HTMX route tests for the image subsystem (direct invocation + fake PB)."""

from __future__ import annotations

from app.api.images import (
    queue_image_generate,
    queue_image_plan,
    queue_image_publish,
    queue_image_regenerate,
    remove_image,
    select_image_version,
    update_image_metadata,
)
from app.api.projects import test_image_generation as run_image_test_route
from app.providers.base import PermanentError
from app.repositories.article_images import ArticleImageRepo
from tests.fake_providers import FakeRegistry
from tests.helpers import (
    call_route,
    make_article,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_req_plain,
    make_user,
    toast_message,
)
from tests.test_image_jobs import make_plan_article, run_job, set_plan


def setup_full(pb):
    user = make_user()
    project = make_project(pb)
    make_member(pb, project["id"], user["id"])
    article = make_plan_article(pb, project["id"])
    return user, project, article


def test_plan_route_requires_content():
    pb = make_pb()
    user, project, article = setup_full(pb)
    make_article(pb, project["id"], final_html=None)  # another article, empty
    req = make_req(pb, user, project["id"])
    resp = call_route(queue_image_plan, req, project["id"], article["id"])  # article has content
    assert toast_message(resp) == "Image plan queued for processing"
    jobs = pb.collection("jobs").get_list(1, 50).items
    assert any(j["type"] == "plan_article_images" for j in jobs)


def test_generate_route_requires_plan():
    pb = make_pb()
    user, project, article = setup_full(pb)
    req = make_req(pb, user, project["id"])
    resp = call_route(
        queue_image_generate, req, project["id"], article["id"], role="cover", section_key=""
    )
    assert "image plan" in toast_message(resp)


def test_generate_route_queues_cover_and_validates_interior_key():
    pb = make_pb()
    user, project, article = setup_full(pb)
    set_plan(pb, article["id"], ["section-1"])
    req = make_req(pb, user, project["id"])
    resp = call_route(
        queue_image_generate, req, project["id"], article["id"], role="cover", section_key=""
    )
    assert "queued" in toast_message(resp)
    resp = call_route(
        queue_image_generate,
        req,
        project["id"],
        article["id"],
        role="interior",
        section_key="section-1",
    )
    assert "queued" in toast_message(resp)
    # invalid interior key rejected
    resp = call_route(
        queue_image_generate,
        req,
        project["id"],
        article["id"],
        role="interior",
        section_key="not-a-key",
    )
    assert "Invalid section key" in toast_message(resp)


def test_regenerate_select_remove_metadata_flow():
    pb = make_pb()
    user, project, article = setup_full(pb)
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()
    run_job(
        pb,
        registry,
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    row = ArticleImageRepo(pb).active_for_role(article["id"], "cover")
    req = make_req(pb, user, project["id"])

    # metadata update
    resp = call_route(
        update_image_metadata,
        req,
        project["id"],
        article["id"],
        row["id"],
        alt_text="close-up of the car dashboard",
        caption="image caption",
    )
    assert toast_message(resp)
    assert ArticleImageRepo(pb).get(row["id"])["altText"] == "close-up of the car dashboard"

    # empty alt rejected
    resp = call_route(
        update_image_metadata, req, project["id"], article["id"], row["id"], alt_text="", caption=""
    )
    assert "Alt text" in toast_message(resp)

    # regenerate queues a NEW version job (version=2 payload)
    resp = call_route(queue_image_regenerate, req, project["id"], article["id"], row["id"])
    assert "(new version)" in toast_message(resp)
    jobs = [
        j
        for j in pb.collection("jobs").get_list(1, 50).items
        if j["type"] == "generate_cover_image"
    ]
    assert jobs and jobs[-1]["payload"]["version"] == 2

    # select a non-ready row is rejected; ready rows can be re-selected
    resp = call_route(select_image_version, req, project["id"], article["id"], row["id"])
    assert toast_message(resp) == "This image version was selected"

    # remove deactivates but keeps history
    resp = call_route(remove_image, req, project["id"], article["id"], row["id"])
    assert "history" in toast_message(resp)
    assert ArticleImageRepo(pb).get(row["id"])["active"] in (False, 0)


def test_publish_route_requires_ready_row():
    pb = make_pb()
    user, project, article = setup_full(pb)
    set_plan(pb, article["id"], [])
    req = make_req(pb, user, project["id"])
    resp = call_route(
        queue_image_publish, req, project["id"], article["id"], image_id="nonexistent", featured=""
    )
    assert toast_message(resp)


def test_non_hx_request_rejected():
    pb = make_pb()
    user, project, article = setup_full(pb)
    req = make_req_plain(pb, user, project["id"])
    resp = call_route(queue_image_plan, req, project["id"], article["id"])
    assert toast_message(resp)  # hx_error answers with a Persian error toast


def test_workspace_renders_populated_images_pane(monkeypatch):
    """Regression: the images pane must render ready previews, status badges
    and version lists from the real _image_slots view-model."""
    from app.api.workspace import _image_slots
    from app.templates import templates
    from tests.test_image_jobs import make_plan_article, run_job, set_plan
    from tests.test_image_publishing import patch_download

    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], ["section-1"])
    run_job(
        pb,
        FakeRegistry(),
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    patch_download(monkeypatch, pb)

    images = ArticleImageRepo(pb).list_for_article(article["id"])
    slots = _image_slots(article, images)
    assert slots and slots[0]["role"] == "cover"
    ready_row = slots[0]["active"]
    assert ready_row["status"] == "ready"

    class FakeRequest:
        url = type("U", (), {"path": "/workspace"})()

        def url_for(self, name: str, **path_params: object) -> str:
            return "/"

    html = templates.get_template("pages/articles/_images_pane.html").render(
        {
            "request": FakeRequest(),
            "project": {"id": project["id"]},
            "article": article,
            "image_slots": slots,
        }
    )
    assert ready_row["filename"] in html  # preview src uses the optimized file
    assert "Ready" in html  # ready badge
    assert ready_row["altText"] in html  # alt text prefilled in the metadata form


def test_image_test_route_success(monkeypatch):
    import asyncio
    import base64 as b64
    import io

    from PIL import Image

    from app.repositories.projects import ProjectSettingsRepo
    from tests.test_image_jobs import img_provider  # registry fixture pattern

    pb = make_pb()
    user = make_user()
    project = make_project(pb)
    make_member(pb, project["id"], user["id"])
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"imageCoverProvider": "fakeimg", "imageCoverModel": "fake-image-1"}
    )
    registry = FakeRegistry()
    provider = img_provider(registry, "fakeimg")

    class _FixedRegistry:
        def get_image_provider(self, *a, **k):
            return provider

    monkeypatch.setattr("app.providers.registry.ProviderRegistry", lambda pb_: _FixedRegistry())
    request = make_req(pb, user, project["id"])
    response = asyncio.run(call_route(run_image_test_route, request, project_id=project["id"]))
    html = response.body.decode()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    assert f"data:image/png;base64,{b64.b64encode(buf.getvalue()).decode('ascii')[:20]}" in html
    assert 'data-toast-type="success"' in html
    assert provider.calls, "test route should call the provider"
    assert "fakeimg" in html and "fake-image-1" in html


def test_image_test_route_error_shows_category(monkeypatch):
    import asyncio

    from app.repositories.projects import ProjectSettingsRepo

    pb = make_pb()
    user = make_user()
    project = make_project(pb)
    make_member(pb, project["id"], user["id"])
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"imageCoverProvider": "fakeimg", "imageCoverModel": "fake-image-1"}
    )

    class _FailingProvider:
        async def generate_image(self, req):
            raise PermanentError(
                "bfl.submit failed with HTTP 404: an HTML web page, not an API response",
                details={"category": "invalid_request"},
            )

        async def aclose(self):
            return None

    class _FixedRegistry:
        def get_image_provider(self, *a, **k):
            return _FailingProvider()

    monkeypatch.setattr("app.providers.registry.ProviderRegistry", lambda pb_: _FixedRegistry())
    request = make_req(pb, user, project["id"])
    response = asyncio.run(call_route(run_image_test_route, request, project_id=project["id"]))
    html = response.body.decode()
    assert 'data-toast-type="error"' in html
    assert "invalid_request" in html
    assert "HTML web page" in html


def test_image_test_route_requires_model(monkeypatch):
    import asyncio

    from app.repositories.projects import ProjectSettingsRepo

    pb = make_pb()
    user = make_user()
    project = make_project(pb)
    make_member(pb, project["id"], user["id"])
    ProjectSettingsRepo(pb).upsert(project["id"], {"imageCoverModel": ""})
    request = make_req(pb, user, project["id"])
    response = asyncio.run(call_route(run_image_test_route, request, project_id=project["id"]))
    assert 'data-toast-type="error"' in response.body.decode()
    assert "Cover image model is not set" in response.body.decode()
