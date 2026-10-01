"""Extensible job registry.

Handlers register themselves with `@register_job("job_type")` at import time.
The registry is the single place that maps job types to handlers — never a
giant switch statement. Adding a job type = implement a handler function and
decorate it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.jobs.context import JobContext

_registry: dict[str, Callable[[JobContext], Any]] = {}
_registry_lock = threading.RLock()
_loaded = False

# job types (documented contract)
JOB_TYPES = (
    "index_project",
    "index_document",
    "write_article",
    "generate_outline",
    "generate_section",
    "assemble_article",
    "publish_article",
    "retry_failed_job",
    "plan_article_images",
    "generate_article_image",
    "generate_cover_image",
    "generate_interior_image",
    "optimize_article_image",
    "publish_article_image",
    "wordpress_sync",
    "research_run",
)


def register_job(
    job_type: str,
) -> Callable[[Callable[[JobContext], Any]], Callable[[JobContext], Any]]:
    """Decorator: register a handler for a job type (idempotent)."""

    def decorator(fn: Callable[[JobContext], Any]) -> Callable[[JobContext], Any]:
        with _registry_lock:
            _registry[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> Callable[[JobContext], Any] | None:
    with _registry_lock:
        return _registry.get(job_type)


def list_job_types() -> list[str]:
    with _registry_lock:
        return sorted(_registry)


def ensure_registered() -> None:
    """Import every module that registers handlers (idempotent, thread-safe)."""
    global _loaded
    if _loaded:
        return
    with _registry_lock:
        if _loaded:
            return
        # Importing these modules triggers @register_job side effects.
        from app.services import (  # noqa: F401
            image_planning,
            images,
            indexing,
            publishing_service,
            research,
            retry_service,
            wordpress_sync,
            writing,
        )

        _loaded = True
