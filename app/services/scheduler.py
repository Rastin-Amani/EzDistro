"""Schedule poller — creates jobs for due schedules (interval-based).

Idempotency: the idempotency key is derived from the schedule's time WINDOW
(now // interval), so a restart within the same window never duplicates a job.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import structlog

from app.repositories.jobs import JobRepo, now_utc
from app.repositories.projects import ProjectRepo
from app.repositories.schedules import ScheduleRepo
from app.repositories.topics import TopicRepo

logger = structlog.get_logger("worker.scheduler")


def run_schedule_poll(pb: Any) -> int:
    schedules = ScheduleRepo(pb).due()
    jobs = JobRepo(pb)
    projects = ProjectRepo(pb)
    created = 0

    for schedule in schedules:
        project_id = schedule["project"]
        project = projects.get(project_id)
        if not project or project.get("status", "active") != "active":
            continue

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
                continue
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
        else:
            logger.warning(
                "schedule with unknown kind skipped", schedule_id=schedule["id"], kind=kind
            )
            continue

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
        created += 1
        logger.info("schedule fired", schedule_id=schedule["id"], kind=kind, project=project_id)

    return created
