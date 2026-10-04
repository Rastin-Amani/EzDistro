"""Job execution context — what handlers receive.

Handlers get progress/event/cancellation primitives and a lazily-assembled
provider stack; they never touch PocketBase directly except through repos
they construct from `ctx.pb`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    PublisherProvider,
    RerankerProvider,
    VectorStoreProvider,
)
from app.repositories.jobs import JobEventRepo
from app.services.settings import ProjectConfig


class JobCancelled(Exception):
    """Raised inside handlers at checkpoints when cancellation was requested."""


@dataclass
class ProviderStack:
    """Lazily-built provider stack — providers are constructed on first access.

    A job that only publishes never pays for (or fails on) missing LLM/embedding
    credentials; each accessor builds its provider exactly once and `aclose`
    shuts down whatever was actually built. Every built provider receives the
    stack's call observer (observability) and — when a limit semaphore is given
    — is wrapped in a bounded-concurrency proxy (rate limiting).
    """

    registry: Any
    config: ProjectConfig
    observer: Any = None  # CallObserver
    limits: dict[str, Any] | None = None  # {"llm"|"embedding"|"publish": asyncio.Semaphore}

    def __post_init__(self) -> None:
        self._llm: dict[str, Any] = {}
        self._embedding: Any = None
        self._vector: Any = None
        self._reranker: Any = None
        self._publisher: Any = None

    def _build(self, getter: Any, **extra: Any) -> Any:
        return getter(self.config.project, self.config.settings, self.observer, **extra)

    @property
    def llm(self) -> LLMProvider:
        """The OUTLINE model (default role)."""
        return self.llm_for("outline")

    def llm_for(self, role: str) -> LLMProvider:
        """Role-specific LLM (outline | section | meta | review) — built once."""
        if role not in self._llm:
            provider = self._build(
                self.registry.get_llm_provider,
                role=role,
                role_config=self.config.role_llm(role),
            )
            sem = (self.limits or {}).get("llm")
            if sem is not None:
                from app.jobs.concurrency import BoundedLLM

                provider = BoundedLLM(provider, sem)
            self._llm[role] = provider
        return self._llm[role]

    @property
    def embedding(self) -> EmbeddingProvider:
        if self._embedding is None:
            provider = self._build(self.registry.get_embedding_provider)
            sem = (self.limits or {}).get("embedding")
            if sem is not None:
                from app.jobs.concurrency import BoundedEmbedding

                provider = BoundedEmbedding(provider, sem)
            self._embedding = provider
        return self._embedding

    @property
    def vector(self) -> VectorStoreProvider:
        if self._vector is None:
            self._vector = self._build(self.registry.get_vector_provider)
        return self._vector

    @property
    def reranker(self) -> RerankerProvider | None:
        if self._reranker is None:
            self._reranker = self._build(self.registry.get_reranker_provider)
        return self._reranker

    @property
    def publisher(self) -> PublisherProvider:
        if self._publisher is None:
            provider = self._build(self.registry.get_publisher_provider)
            sem = (self.limits or {}).get("publish")
            if sem is not None:
                from app.jobs.concurrency import BoundedPublisher

                provider = BoundedPublisher(provider, sem)
            self._publisher = provider
        return self._publisher

    async def aclose(self) -> None:
        for provider in list(self._llm.values()) + [
            self._embedding,
            self._vector,
            self._reranker,
            self._publisher,
        ]:
            if provider is None:
                continue
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


@dataclass
class JobContext:
    pb: Any
    job: dict[str, Any]
    config: ProjectConfig
    providers: ProviderStack
    registry: Any  # ProviderRegistry — extra provider building (e.g. WP publisher)
    events: JobEventRepo
    set_progress: Callable[..., None]
    request_cancel: Callable[[str], bool]

    @property
    def job_id(self) -> str:
        return self.job["id"]

    @property
    def project_id(self) -> str:
        return self.job["project"]

    # -- progress & events ----------------------------------------------------------
    def progress(
        self,
        pct: int,
        *,
        stage: str = "",
        message: str = "",
        current: int = 0,
        total: int = 0,
    ) -> None:
        """Structured progress: percentage + stage + message + current/total items.

        Example: progress(35, stage="writing_sections", message="Section 7 of 20",
        current=7, total=20)
        """
        self.set_progress(
            self.job_id, pct, stage=stage, message=message, current_item=current, total_items=total
        )

    def stage_started(
        self, stage: str, message: str = "", data: dict[str, Any] | None = None
    ) -> None:
        self.event(
            "job.stage_started",
            message or f"stage started: {stage}",
            {"stage": stage, **(data or {})},
        )

    def stage_completed(
        self, stage: str, message: str = "", data: dict[str, Any] | None = None
    ) -> None:
        self.event(
            "job.stage_completed",
            message or f"stage completed: {stage}",
            {"stage": stage, **(data or {})},
        )

    def provider_error(self, message: str, data: dict[str, Any] | None = None) -> None:
        self.event("job.provider_error", message, data)

    def event(self, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.events.add(
            project=self.project_id,
            job=self.job_id,
            event_type=level,
            message=message,
            metadata=data,
        )

    def info(self, message: str, data: dict[str, Any] | None = None) -> None:
        self.event("info", message, data)

    def warning(self, message: str, data: dict[str, Any] | None = None) -> None:
        self.event("warning", message, data)

    def error_event(self, message: str, data: dict[str, Any] | None = None) -> None:
        self.event("error", message, data)

    # -- cancellation ---------------------------------------------------------------
    async def check_cancelled(self) -> None:
        """Cooperative cancellation checkpoint — poll the job record, raise when asked."""
        cancelled = self.request_cancel(self.job_id)
        if cancelled:
            raise JobCancelled("job cancelled by user")

    def payload(self) -> dict[str, Any]:
        return self.job.get("payload") or {}
