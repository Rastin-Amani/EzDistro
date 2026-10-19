"""Schedule poller — creates jobs for due schedules (interval-based).

Idempotency: the idempotency key is derived from the schedule's time WINDOW
(now // interval), so a restart within the same window never duplicates a job.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import structlog

from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo, now_utc
from app.repositories.projects import ProjectRepo
from app.repositories.schedules import ScheduleRepo
from app.repositories.topics import TopicRepo
from app.services.settings import ProjectConfig

logger = structlog.get_logger("worker.scheduler")


def run_schedule_poll(pb: Any) -> int:
    schedules = ScheduleRepo(pb).due()
    jobs = JobRepo(pb)
    projects = ProjectRepo(pb)
    created = 0
    failed = 0
    logger.info("schedule.poll.started", due=len(schedules))

    for schedule in schedules:
        # ONE failing schedule must never abort the poll for the others —
        # a single bad query used to kill every job in the cycle.
        try:
            if _fire_schedule(pb, jobs, projects, schedule):
                created += 1
        except Exception as exc:
            failed += 1
            logger.exception(
                "schedule.fire.error",
                schedule_id=schedule.get("id"),
                kind=schedule.get("kind"),
                error=str(exc),
            )

    _write_heartbeat(pb, due=len(schedules), created=created, failed=failed)
    logger.info("schedule.poll.completed", due=len(schedules), created=created, failed=failed)

    # Same cadence, same workers: finish what a lost job left behind. Cheap
    # (one query per in-progress state) and idempotent, so N workers are fine.
    try:
        from app.services.reaper import reconcile_stuck

        recovered = reconcile_stuck(pb)
        if recovered:
            logger.info("reaper.completed", **recovered)
    except Exception:
        logger.exception("reaper.error")
    return created


def _fire_schedule(pb: Any, jobs: JobRepo, projects: ProjectRepo, schedule: dict[str, Any]) -> bool:
    """Fire one schedule; returns True when a job was created."""
    project_id = schedule["project"]
    project = projects.get(project_id)
    if not project or project.get("status", "active") != "active":
        logger.info(
            "schedule skipped: project not active",
            schedule_id=schedule.get("id"),
            kind=schedule.get("kind"),
            project=project_id,
        )
        return False

    interval_minutes = max(1, int(schedule.get("intervalMinutes") or 60))
    window = int(time.time() // (interval_minutes * 60))
    kind = schedule.get("kind")

    if kind == "index":
        idempotency_key = f"index:project:{project_id}:{schedule['id']}:{window}"
        job_type, payload, entity = (
            "index_project",
            {"trigger": "schedule", "scheduleId": schedule["id"]},
            ("project", project_id),
        )
    elif kind == "write":
        # The write schedule targets the next planned topic (highest
        # priority, oldest first). Without a topic there is nothing to
        # write — advance the schedule and skip, instead of enqueueing a
        # job that could never produce content.
        topic = TopicRepo(pb).next_unwritten(project_id)
        if topic is None:
            next_run = now_utc() + dt.timedelta(minutes=interval_minutes)
            ScheduleRepo(pb).mark_run(schedule["id"], next_run)
            logger.info(
                "schedule fired without planned topics",
                schedule_id=schedule["id"],
                kind=kind,
                project=project_id,
            )
            return False
        idempotency_key = f"write:article:{topic['id']}:{window}"
        job_type, payload, entity = (
            "write_article",
            {
                "trigger": "schedule",
                "scheduleId": schedule["id"],
                "topicId": topic["id"],
            },
            ("topic", topic["id"]),
        )
    elif kind == "publish":
        # The publish schedule drains the ready queue: one article per fire,
        # oldest first, above the auto-publish score floor. Publishing chains
        # the RAG reindex and the inbound link-building job.
        auto = ProjectConfig.load(pb, project_id).auto_publish
        article = None
        if auto["enabled"]:
            article = ArticleRepo(pb).next_to_publish(project_id, int(auto["min_score"]))
        if article is None:
            next_run = now_utc() + dt.timedelta(minutes=interval_minutes)
            ScheduleRepo(pb).mark_run(schedule["id"], next_run)
            logger.info(
                "schedule fired without a publishable article",
                schedule_id=schedule["id"],
                kind=kind,
                project=project_id,
                auto_publish=auto["enabled"],
            )
            return False
        idempotency_key = f"publish:article:{article['id']}:{window}"
        # Picking it IS the approval (the score floor above already gated it),
        # exactly like the auto-publish path in the assembler does.
        ArticleRepo(pb).set_status(article["id"], "approved")
        job_type, payload, entity = (
            "publish_article",
            {
                "trigger": "schedule",
                "scheduleId": schedule["id"],
                "articleId": article["id"],
                "action": "publish",
            },
            ("article", article["id"]),
        )
    else:
        logger.warning("schedule with unknown kind skipped", schedule_id=schedule["id"], kind=kind)
        return False

    jobs.create(
        project=project_id,
        type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        max_attempts=3,
        entity_type=entity[0],
        entity_id=entity[1],
    )
    next_run = now_utc() + dt.timedelta(minutes=interval_minutes)
    ScheduleRepo(pb).mark_run(schedule["id"], next_run)
    logger.info("schedule fired", schedule_id=schedule["id"], kind=kind, project=project_id)
    return True


def _write_heartbeat(pb: Any, *, due: int, created: int, failed: int) -> None:
    """Persist the poll outcome so the WEB UI can show worker scheduler status.

    Diagnostics only — never allowed to break scheduling.
    """
    try:
        from app.repositories.jobs import now_utc, pb_dt

        payload = {
            "lastPollAt": pb_dt(now_utc()),
            "due": due,
            "created": created,
            "failed": failed,
        }
        try:
            record = pb.collection("app_settings").get_first_list_item(
                'key="scheduler_heartbeat"', {"perPage": 1}
            )
        except Exception:
            record = None
        if record is not None:
            pb.collection("app_settings").update(record.id, {"value": payload})
        else:
            pb.collection("app_settings").create({"key": "scheduler_heartbeat", "value": payload})
    except Exception:
        pass
