"""Worker process entrypoint.

Runs independently of the web process:
    python -m app.workers.worker

Responsibilities:
- poll for pending/retrying/due jobs and expired leases, claim atomically,
  execute handlers (correctness never depends on in-memory state)
- run due schedules → create jobs (idempotency keys prevent duplicates)
- bounded provider concurrency (LLM / embeddings / publishing rate limits)
- structured logging with job_id correlation
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings  # noqa: E402
from app.jobs.engine import JobEngine  # noqa: E402
from app.jobs.handlers import ensure_registered  # noqa: E402
from app.logging_config import logger  # noqa: E402
from app.pb import get_admin_pb  # noqa: E402
from app.services.scheduler import run_schedule_poll  # noqa: E402

POLL_INTERVAL = max(1.0, settings.poll_interval_seconds)
SCHEDULE_INTERVAL = max(10.0, settings.schedule_poll_interval_seconds)


async def main() -> None:
    worker_id = settings.derived_worker_id
    logger.info(
        "worker.starting",
        worker_id=worker_id,
        pb_url=settings.pb_url,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        llm_concurrency=settings.llm_concurrency,
        embedding_concurrency=settings.embedding_concurrency,
        publish_concurrency=settings.publish_concurrency,
    )

    pb = get_admin_pb()
    ensure_registered()
    from app.jobs.handlers import get_handler

    engine = JobEngine(
        pb,
        worker_id=worker_id,
        lease_seconds=settings.lease_seconds,
        heartbeat_interval=settings.heartbeat_interval_seconds,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        llm_concurrency=settings.llm_concurrency,
        embedding_concurrency=settings.embedding_concurrency,
        publish_concurrency=settings.publish_concurrency,
        handlers={
            job_type: get_handler(job_type)
            for job_type in (
                "index_project",
                "index_document",
                "write_article",
                "generate_outline",
                "generate_section",
                "assemble_article",
                "publish_article",
                "retry_failed_job",
            )
        },
    )

    stop = asyncio.Event()

    def _signal_handler(*_args: object) -> None:
        logger.info("worker.stopping", worker_id=worker_id)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    async def job_poll_loop() -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                await engine.poll_and_run()
            except Exception:
                logger.exception("worker.poll.error")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, POLL_INTERVAL - elapsed))

    async def schedule_poll_loop() -> None:
        while not stop.is_set():
            try:
                created = run_schedule_poll(pb)
                if created:
                    logger.info("scheduler.created_jobs", count=created)
            except Exception:
                logger.exception("worker.schedule.error")
            await asyncio.sleep(SCHEDULE_INTERVAL)

    async def metrics_flush_loop() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            try:
                written = engine.flush_metrics()
                if written:
                    logger.info("worker.metrics.flushed", rows=written)
            except Exception:
                logger.exception("worker.metrics.flush.error")
            # close pooled provider clients idle too long (connection leak guard)
            try:
                from app.providers.http import sweep_idle_clients

                swept = sweep_idle_clients()
                if swept:
                    logger.info("worker.idle_clients.swept", closed=swept)
            except Exception:
                logger.exception("worker.idle_clients.sweep.error")

    tasks = [
        asyncio.create_task(job_poll_loop()),
        asyncio.create_task(schedule_poll_loop()),
        asyncio.create_task(metrics_flush_loop()),
    ]
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("worker.stopped", worker_id=worker_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
