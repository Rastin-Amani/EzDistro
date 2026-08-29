"""Route tests — articles: detail/workspace/outline/sections, status transitions,
review workflow (approve/send-back), regenerate, publish/update/unpublish,
publish-run retry, rollback.

Covers previously untested routes plus the state-machine guards.
"""

from __future__ import annotations

import pytest

from app.api import articles as A
from app.api import workspace as W
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo
from app.repositories.members import MemberRepo
from app.repositories.publishing_runs import PublishingRunRepo
from tests.fakes import default_unique_fields
from tests.helpers import (
    call_route,
    make_article,
    make_member,
    make_pb,
    make_project,
    make_publish_run,
    make_req,
    make_section,
    make_topic,
    make_user,
    toast_message,
)


def _setup():
    pb = make_pb()
    proj_a = make_project(pb, slug="proj-a", name="A")
    proj_b = make_project(pb, slug="proj-b", name="B")
    MemberRepo(pb).add(project=proj_a["id"], user="u1", role="owner")
    MemberRepo(pb).add(project=proj_b["id"], user="u2", role="owner")
    return pb, proj_a, proj_b


# ---------------------------------------------------------------------------
# Read pages
# ---------------------------------------------------------------------------
def test_article_detail_renders():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.article_detail, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    assert "عنوان مقاله" in resp.body.decode()


def test_article_detail_foreign_article_not_found_page():
    pb, proj_a, proj_b = _setup()
    foreign = make_article(pb, proj_b["id"], final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.article_detail, req, proj_a["id"], foreign["id"])
    assert resp.status_code == 200
    assert "یافت نشد" in resp.body.decode()


def test_article_workspace_renders_with_outline():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.article_workspace, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "مقدمه" in body  # outline section heading rendered


def test_outline_pane_fragment_renders():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.article_outline_pane, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    assert "مقدمه" in resp.body.decode()


def test_section_status_fragment_renders():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    section = SectionRepo(pb).list_for_article(article["id"])[0]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.section_status, req, proj_a["id"], article["id"], section["id"])
    assert resp.status_code == 200
    assert "انجام شد" in resp.body.decode()  # status badge fragment


def test_review_page_renders_validation_report():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], final_html="<p>محتوا</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.article_review, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    assert "بازبینی" in resp.body.decode()


def test_review_page_never_white_screens_on_internal_error(monkeypatch):
    """A data/render error in the review pipeline must surface a friendly
    error page (200 + Persian message), never a blank 500."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], final_html="<p>محتوا</p>")
    req = make_req(pb, make_user(), proj_a["id"])

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.api.workspace._live_validation", boom)
    resp = call_route(W.article_review, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "مشکلی در بارگذاری صفحه بازبینی" in body
    assert "بازگشت به مقاله" in body


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------
def test_set_article_status_valid_transition():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="review")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        W.set_article_status, req, proj_a["id"], article["id"], status="ready_to_publish"
    )
    assert "به‌روزرسانی" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "ready_to_publish"


def test_set_article_status_rejects_premature_ready_to_publish():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="draft")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        W.set_article_status, req, proj_a["id"], article["id"], status="ready_to_publish"
    )
    assert "آماده انتشار نیست" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "draft"


def test_set_article_status_rejects_invalid_status():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="review")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.set_article_status, req, proj_a["id"], article["id"], status="bogus")
    assert "ناموفق" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "review"


def test_approve_article_passes_validation():
    pb, proj_a, _ = _setup()
    words = " ".join(f"کلمه {i}" for i in range(320))
    article = make_article(
        pb,
        proj_a["id"],
        status="review",
        final_html=f"<h2>مقدمه</h2><p>{words}</p>",
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.approve_article, req, proj_a["id"], article["id"])
    assert "تأیید شد" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "approved"


def test_approve_article_refused_when_not_review():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="draft")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.approve_article, req, proj_a["id"], article["id"])
    assert "بازبینی" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "draft"


def test_approve_article_refused_when_validation_fails():
    pb, proj_a, _ = _setup()
    # no finalHtml → validation cannot pass
    article = make_article(pb, proj_a["id"], status="review", final_html="")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.approve_article, req, proj_a["id"], article["id"])
    assert "اعتبارسنجی" in toast_message(resp) or "پاس نکرد" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "review"


def test_send_back_article_records_note():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="review")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.send_back_article, req, proj_a["id"], article["id"], note="بازنویسی مقدمه")
    assert "بازگردانده شد" in toast_message(resp)
    updated = ArticleRepo(pb).get(article["id"])
    assert updated["status"] == "sent_back"
    assert updated["reviewNote"] == "بازنویسی مقدمه"


def test_send_back_article_refused_from_wrong_state():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="draft")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.send_back_article, req, proj_a["id"], article["id"], note="x")
    assert "قابل بازگشت نیست" in toast_message(resp)
    assert ArticleRepo(pb).get(article["id"])["status"] == "draft"


# ---------------------------------------------------------------------------
# Outline editing
# ---------------------------------------------------------------------------
def test_outline_add_section():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        W.outline_add,
        req,
        proj_a["id"],
        article["id"],
        heading="بخش جدید",
        content_brief="خلاصه جدید",
    )
    assert "اضافه شد" in toast_message(resp)
    sections = SectionRepo(pb).list_for_article(article["id"])
    assert len(sections) == 3
    assert sections[-1]["heading"] == "بخش جدید"


def test_outline_add_requires_heading():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.outline_add, req, proj_a["id"], article["id"], heading="  ")
    assert "الزامی" in toast_message(resp)
    assert len(SectionRepo(pb).list_for_article(article["id"])) == 2


def test_outline_move_swaps_positions():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.outline_move, req, proj_a["id"], article["id"], 0, direction="down")
    assert "جابه‌جا شد" in toast_message(resp)
    sections = sorted(SectionRepo(pb).list_for_article(article["id"]), key=lambda s: s["position"])
    assert sections[0]["heading"] == "بدنه"


def test_outline_move_invalid_position_errors():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.outline_move, req, proj_a["id"], article["id"], 99, direction="down")
    assert "نامعتبر" in toast_message(resp)


def test_outline_delete_removes_section():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.outline_delete, req, proj_a["id"], article["id"], 0)
    assert "حذف شد" in toast_message(resp)
    assert len(SectionRepo(pb).list_for_article(article["id"])) == 1


def test_outline_update_brief_marks_regeneration():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        W.outline_update_brief,
        req,
        proj_a["id"],
        article["id"],
        0,
        heading="مقدمه",
        content_brief="خلاصه تازه",
    )
    assert "ذخیره شد" in toast_message(resp)
    section = sorted(SectionRepo(pb).list_for_article(article["id"]), key=lambda s: s["position"])[
        0
    ]
    assert section["contentBrief"] == "خلاصه تازه"
    assert section["status"] == "pending"


# ---------------------------------------------------------------------------
# Assemble / regenerate
# ---------------------------------------------------------------------------
def test_queue_assemble_creates_job():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.queue_assemble, req, proj_a["id"], article["id"])
    assert "برنامه‌ریزی شد" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "assemble_article"
    assert jobs[0]["payload"] == {"articleId": article["id"]}


def test_queue_assemble_reuses_active_job():
    """An assemble already pending/running is not duplicated."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    JobRepo(pb).create(
        project=proj_a["id"],
        type="assemble_article",
        payload={"articleId": article["id"]},
        idempotency_key=f"assemble:article:{article['id']}",
        max_attempts=60,
        entity_type="article",
        entity_id=article["id"],
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.queue_assemble, req, proj_a["id"], article["id"])
    assert "در حال ساخت است" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len([j for j in jobs if j["type"] == "assemble_article"]) == 1


def test_queue_assemble_reassembles_after_completion():
    """A completed assemble never blocks a later manual re-assemble."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    completed = JobRepo(pb).create(
        project=proj_a["id"],
        type="assemble_article",
        payload={"articleId": article["id"]},
        idempotency_key=f"assemble:article:{article['id']}",
        max_attempts=60,
        entity_type="article",
        entity_id=article["id"],
    )
    pb.collection("jobs").update(completed["id"], {"status": "completed"})
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.queue_assemble, req, proj_a["id"], article["id"])
    assert "برنامه‌ریزی شد" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len([j for j in jobs if j["type"] == "assemble_article"]) == 2


def test_workspace_generating_shows_progress_and_readonly():
    """During generation the workspace shows pipeline progress and keeps the
    in-flight section's content editor read-only (worker will fill it)."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="generating", final_html="<p>x</p>")
    sections = SectionRepo(pb).list_for_article(article["id"])
    pb.collection("article_sections").update(
        sections[0]["id"], {"status": "pending", "content": ""}
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.article_workspace, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "در حال تولید بخش‌ها" in body
    assert "1 از 2 بخش آماده شد" in body
    assert "disabled" in body  # in-flight section editor is read-only


def test_workspace_generating_all_done_offers_reassemble():
    """Once every section is done the workspace offers a manual re-assemble
    even while the article is still in the generating→assembling window."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="generating", final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.article_workspace, req, proj_a["id"], article["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "بخش‌ها آماده شدند — در حال ساخت مقاله نهایی" in body
    assert "بازسازی" in body  # manual assemble available


def test_regenerate_section_queues_generate_section():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    section = SectionRepo(pb).list_for_article(article["id"])[0]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        A.regenerate_article, req, proj_a["id"], article["id"], section_id=section["id"]
    )
    assert "بازتولید بخش" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "generate_section"
    assert jobs[0]["payload"] == {"sectionId": section["id"]}
    assert ArticleRepo(pb).get(article["id"])["status"] == "generating"


def test_regenerate_full_article_queues_write_job():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.regenerate_article, req, proj_a["id"], article["id"])
    assert "بازتولید مقاله" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "write_article"
    assert jobs[0]["payload"]["regenerate"] is True


def test_regenerate_full_article_rejects_duplicate_inflight():
    """Regression: a second full regeneration while one is already running
    must be rejected (not leave the article stuck in `generating`)."""
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"])
    JobRepo(pb).create(
        project=proj_a["id"],
        type="write_article",
        payload={"topicId": article["topicId"], "regenerate": True},
        idempotency_key="write:article:inflight:1",
        max_attempts=3,
        entity_type="article",
        entity_id=article["id"],
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.regenerate_article, req, proj_a["id"], article["id"])
    assert "در حال بازتولید است" in toast_message(resp)


def test_regenerate_foreign_section_rejected():
    pb, proj_a, proj_b = _setup()
    foreign_article = make_article(pb, proj_b["id"])
    foreign_section = SectionRepo(pb).list_for_article(foreign_article["id"])[0]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        A.regenerate_article,
        req,
        proj_a["id"],
        foreign_article["id"],
        section_id=foreign_section["id"],
    )
    assert "یافت نشد" in toast_message(resp)
    assert SectionRepo(pb).get(foreign_section["id"])["status"] == "done"


# ---------------------------------------------------------------------------
# Publish / update / unpublish
# ---------------------------------------------------------------------------
def test_publish_article_queues_publish_job():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="approved", final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.publish_article, req, proj_a["id"], article["id"])
    assert "انتشار آغاز شد" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "publish_article"
    assert jobs[0]["payload"] == {"articleId": article["id"], "action": "publish"}


def test_publish_article_requires_content():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="approved", final_html=None)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.publish_article, req, proj_a["id"], article["id"])
    assert "محتوایی ندارد" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


def test_publish_article_dedupes_active_job():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="approved", final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    call_route(A.publish_article, req, proj_a["id"], article["id"])
    # second click while the first job is still active → no duplicate job
    call_route(A.publish_article, req, proj_a["id"], article["id"])
    jobs = [
        j
        for j in JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
        if j["type"] == "publish_article"
    ]
    assert len(jobs) == 1


def test_update_article_post_requires_wp_id():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="published", final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.update_article_post, req, proj_a["id"], article["id"])
    assert "منتشر نشده" in toast_message(resp)


def test_update_article_post_queues_update():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="published", final_html="<p>x</p>", wp_id=42)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.update_article_post, req, proj_a["id"], article["id"])
    assert "به‌روزرسانی" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["payload"]["action"] == "update"


def test_unpublish_requires_wp_id():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="published", final_html="<p>x</p>")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.unpublish_article_post, req, proj_a["id"], article["id"])
    assert "در وردپرس نیست" in toast_message(resp)


def test_unpublish_queues_unpublish_job():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="published", final_html="<p>x</p>", wp_id=7)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.unpublish_article_post, req, proj_a["id"], article["id"])
    assert "خصوصی" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["payload"]["action"] == "unpublish"


def test_retry_publish_run_queues_matching_mode():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="failed", final_html="<p>x</p>")
    run = make_publish_run(pb, article["id"], proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(A.retry_publish_run, req, proj_a["id"], article["id"], run["id"])
    assert "تلاش مجدد" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["payload"]["action"] == "publish"


def test_retry_publish_run_foreign_run_rejected():
    pb, proj_a, proj_b = _setup()
    foreign_article = make_article(pb, proj_b["id"], status="failed", final_html="<p>x</p>")
    foreign_run = make_publish_run(pb, foreign_article["id"], proj_b["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        A.retry_publish_run, req, proj_a["id"], foreign_article["id"], foreign_run["id"]
    )
    assert "ناموفق" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


# ---------------------------------------------------------------------------
# Revisions / rollback
# ---------------------------------------------------------------------------
def test_rollback_restores_revision_and_sets_review():
    pb, proj_a, _ = _setup()
    article = make_article(pb, proj_a["id"], status="approved", final_html="<p>جدید</p>")
    # snapshot current state as a revision
    from app.services.revisions import RevisionService

    rev = RevisionService(pb).snapshot(article, "manual", note="before")
    # mutate content
    ArticleRepo(pb).update(article["id"], {"finalHtml": "<p>پس از تغییر</p>"})
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.rollback_article, req, proj_a["id"], article["id"], rev["id"])
    assert "بازگشت به نسخه" in toast_message(resp)
    restored = ArticleRepo(pb).get(article["id"])
    assert restored["status"] == "review"
    assert "<p>جدید</p>" in (restored.get("finalHtml") or "")


def test_rollback_foreign_revision_rejected():
    pb, proj_a, proj_b = _setup()
    foreign_article = make_article(pb, proj_b["id"])
    from app.services.revisions import RevisionService

    rev = RevisionService(pb).snapshot(foreign_article, "manual", note="x")
    article = make_article(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.rollback_article, req, proj_a["id"], article["id"], rev["id"])
    assert "یافت نشد" in toast_message(resp)


# ---------------------------------------------------------------------------
# Cross-tenant + role guards on the article mutations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "route,fn",
    [
        ("approve", W.approve_article),
        ("send-back", W.send_back_article),
        ("assemble", W.queue_assemble),
        ("publish", A.publish_article),
        ("unpublish", A.unpublish_article_post),
    ],
)
def test_article_mutations_reject_foreign_article(route, fn):
    pb, proj_a, proj_b = _setup()
    foreign = make_article(pb, proj_b["id"], final_html="<p>x</p>", wp_id=1)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(fn, req, proj_a["id"], foreign["id"])
    assert "یافت نشد" in toast_message(resp)
    # the foreign article was not touched
    assert ArticleRepo(pb).get(foreign["id"])["status"] == foreign["status"]


@pytest.mark.parametrize(
    "fn",
    [
        W.approve_article,
        W.send_back_article,
        W.queue_assemble,
        A.publish_article,
        A.regenerate_article,
    ],
)
def test_article_mutations_reject_viewer_role(fn):
    pb, proj_a, _ = _setup()
    MemberRepo(pb).add(project=proj_a["id"], user="v1", role="viewer")
    article = make_article(pb, proj_a["id"], status="review", final_html="<p>x</p>")
    req = make_req(pb, make_user("v1"), proj_a["id"])
    resp = call_route(fn, req, proj_a["id"], article["id"])
    assert "ناموفق" in toast_message(resp) or "کافی" in toast_message(resp)
