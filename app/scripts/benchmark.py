"""Load-test scenario for the EzDistro worker + pipelines (fake providers).

Scenario:
- 1 project
- 1,000 indexed documents (retrieval corpus)
- 100 topics
- 10 simultaneous article generations (10 sections each)

Measures: worker throughput (jobs/sec), error rate, observed provider
concurrency vs limits, end-to-end completion time, memory (tracemalloc),
CPU time, and PB query volume. Uses the REAL job engine + REAL handlers with
fake providers, so application overhead is what is measured.

Usage: .venv/bin/python -m app.scripts.benchmark [--jobs 10] [--topics 100]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import tracemalloc
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.jobs.engine import JobEngine  # noqa: E402
from app.jobs.handlers import ensure_registered, get_handler  # noqa: E402
from app.repositories.jobs import JobRepo  # noqa: E402
from app.repositories.projects import DEFAULT_SETTINGS  # noqa: E402
from app.repositories.prompts import PromptRepo  # noqa: E402
from app.repositories.topics import TopicRepo  # noqa: E402
from app.services.metrics import query_provider_metrics  # noqa: E402
from tests.fake_providers import (  # noqa: E402
    FakeLLM,
    FakeRegistry,
)
from tests.fakes import FakePocketBase, default_unique_fields  # noqa: E402

OUTLINE_JSON = (
    '{"title": "\u0645\u0642\u0627\u0644\u0647 \u0634\u0645\u0627\u0631\u0647 {n}", "slug": "article-{n}", "sections": ['
    + ",".join(
        '{"heading": "\u0628\u062e\u0634 {s}", "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0628\u062e\u0634 {s}"}'
        for s in range(1, 11)
    )
    + "]}"
)
SECTION_HTML = (
    "<p>"
    + (
        "\u06a9\u0644\u0645\u0647 \u0645\u062d\u062a\u0648\u0627\u06cc \u0627\u06cc\u0646 \u0628\u062e\u0634 \u0628\u0631\u0627\u06cc \u0622\u0632\u0645\u0648\u0646 \u0628\u0627\u0631 "
        * 40
    ).strip()
    + "</p>"
)


def seed_environment(pb: FakePocketBase, project_id: str, n_topics: int) -> list[str]:
    """Seed prompts, retrieval corpus (1000 docs), and topics."""
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        (
            "outline_user",
            "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646 \u0628\u0631\u0627\u06cc \u0645\u0648\u0636\u0648\u0639 «{{ topic.title }}».",
        ),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646."),
        ("internal_linking", "\u0644\u06cc\u0646\u06a9 \u062f\u0627\u062e\u0644\u06cc."),
    ):
        PromptRepo(pb).save_version(
            project_id=project_id, ptype=ptype, name="default", content=content
        )
    topics: list[str] = []
    for i in range(n_topics):
        topic = TopicRepo(pb).create(
            project=project_id,
            title=f"\u0645\u0648\u0636\u0648\u0639 {i}",
            keyword=f"\u06a9\u0644\u06cc\u062f {i}",
        )
        topics.append(topic["id"])
    return topics


class SyntheticLLM(FakeLLM):
    """Synthesizes responses: outline JSON for outline prompts, HTML for sections."""

    def __init__(self, delay: float = 0.01, n_sections: int = 10) -> None:
        super().__init__([], delay=delay)
        self.n_sections = n_sections

    def _next(self, json_mode: bool, user: str = "") -> str:
        self.calls.append({"json_mode": json_mode, "user": user})
        if "JSON" in user or "json" in user:
            sections = ",".join(
                f'{{"heading": "\u0628\u062e\u0634 {s}", "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0628\u062e\u0634 {s}"}}'
                for s in range(1, self.n_sections + 1)
            )
            return '{"title": "Article", "slug": "article", "sections": [' + sections + "]}"
        return SECTION_HTML


class LoadRegistry(FakeRegistry):
    """Fake registry with instrumented LLM (active concurrency + latency)."""

    def __init__(self, delay: float = 0.01, n_sections: int = 10) -> None:
        super().__init__()
        self.llm = SyntheticLLM(delay=delay, n_sections=n_sections)

    def get_llm_provider(
        self, project, settings, observer=None, integration=None, role="outline", role_config=None
    ):
        if observer is not None:
            setter = getattr(self.llm, "_set_observer", None)
            if setter is not None:
                setter(observer)
        return self.llm


async def run_load_test(
    *, n_jobs: int, n_topics: int, llm_delay: float, llm_concurrency: int
) -> dict[str, Any]:
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = pb.collection("projects").create(
        {
            "name": "\u067e",
            "slug": "bench",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    topic_ids = seed_environment(pb, project["id"], n_topics)

    registry = LoadRegistry(delay=llm_delay)
    registry.vector.points = {
        f"bench:{i}:0": __import__("app.providers.base", fromlist=["VectorPoint"]).VectorPoint(
            id=f"bench:{i}:0",
            vector=[0.1] * 8,
            payload={
                "project_id": project["id"],
                "source_url": f"https://site.test/{i}",
                "title": f"\u0645\u0633\u062a\u0646\u062f {i}",
                "chunk_text": "\u0645\u062a\u0646 \u0645\u0631\u062a\u0628\u0637 \u0628\u0631\u0627\u06cc \u0628\u0627\u0632\u06cc\u0627\u0628\u06cc",
            },
        )
        for i in range(1000)
    }

    engine = JobEngine(
        pb,
        worker_id="bench-worker",
        lease_seconds=300,
        heartbeat_interval=60.0,
        max_concurrent_jobs=n_jobs,
        llm_concurrency=llm_concurrency,
        embedding_concurrency=2,
        publish_concurrency=1,
        handlers={
            t: get_handler(t)
            for t in (
                "write_article",
                "generate_section",
                "assemble_article",
                "publish_article",
                "retry_failed_job",
            )
        },
    )
    engine.registry = registry  # fake providers instead of real integrations

    # queue 10 simultaneous article generations
    for i in range(n_jobs):
        topic_id = topic_ids[i % len(topic_ids)]
        JobRepo(pb).create(
            project=project["id"],
            type="write_article",
            payload={"topicId": topic_id},
            idempotency_key=f"bench:write:{i}",
            max_attempts=3,
        )

    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    queries_before = pb.storage.query_count

    # run the engine loop until the WHOLE batch (write + sections + assemble) completes
    async def run_until_done() -> None:
        while True:
            await engine.poll_and_run()
            await asyncio.sleep(0.01)  # let claimed tasks finish in this loop
            remaining = len(
                pb.collection("jobs").get_full_list(
                    {"filter": '(status="pending" || status="retrying" || status="running")'}
                )
            )
            if remaining == 0:
                break

    await run_until_done()
    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    _, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    engine.flush_metrics()
    jobs = pb.collection("jobs").get_full_list()
    statuses = [j["status"] for j in jobs]
    completed = sum(1 for s in statuses if s == "completed")
    failed = sum(1 for s in statuses if s == "failed")
    failed_errors = [
        f"{j['type']}: {j.get('errorCode')} — {str(j.get('errorMessage'))[:100]}"
        for j in jobs
        if j["status"] == "failed"
    ][:5]
    provider_metrics = query_provider_metrics(pb, project_id=project["id"])

    return {
        "n_jobs": n_jobs,
        "wall_seconds": round(wall, 2),
        "cpu_seconds": round(cpu, 2),
        "jobs_completed": completed,
        "jobs_failed": failed,
        "error_rate": round(failed / max(1, completed + failed), 3),
        "throughput_jobs_per_sec": round((completed + failed) / wall, 2),
        "llm_calls": len(registry.llm.calls),
        "llm_max_active": registry.llm.max_active,
        "llm_concurrency_limit": llm_concurrency,
        "pb_queries": pb.storage.query_count - queries_before,
        "peak_memory_mb": round(mem_peak / (1024 * 1024), 1),
        "provider_metric_rows": len(provider_metrics),
        "sample_errors": failed_errors,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--topics", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.01, help="simulated LLM latency (s)")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    result = asyncio.run(
        run_load_test(
            n_jobs=args.jobs,
            n_topics=args.topics,
            llm_delay=args.delay,
            llm_concurrency=args.concurrency,
        )
    )
    print("\n=== EzDistro load-test report ===")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
