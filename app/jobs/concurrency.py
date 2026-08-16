"""Bounded concurrency wrappers — rate limits for LLM, embeddings and publishing.

Each wrapper holds an asyncio.Semaphore; every provider call acquires it before
reaching the adapter. Limits are configurable per process (env) and optionally
per project (rate_limits in project_settings) — see app/jobs/engine.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.providers.base import GenerationParams, LLMResult, ModelInfo, PublishResult, WPPost


class BoundedLLM:
    """Rate-limited LLMProvider proxy."""

    def __init__(self, inner: Any, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._sem = semaphore
        self.provider_name = getattr(inner, "provider_name", "")
        self.model_name = getattr(inner, "model_name", "")
        self.model_info: ModelInfo | None = getattr(inner, "model_info", None)
        self.timeout = getattr(inner, "timeout", 120.0)

    async def generate(
        self, *, system=None, user: str, params: GenerationParams | None = None
    ) -> LLMResult:
        async with self._sem:
            return await self._inner.generate(system=system, user=user, params=params)

    async def generate_json(
        self, *, system=None, user: str, params: GenerationParams | None = None
    ) -> dict[str, Any]:
        async with self._sem:
            return await self._inner.generate_json(system=system, user=user, params=params)

    async def stream(
        self, *, system=None, user: str, params: GenerationParams | None = None
    ) -> AsyncIterator[str]:
        async with self._sem:
            async for chunk in self._inner.stream(system=system, user=user, params=params):
                yield chunk

    async def ping(self) -> None:
        async with self._sem:
            await self._inner.ping()

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


class BoundedEmbedding:
    """Rate-limited EmbeddingProvider proxy."""

    def __init__(self, inner: Any, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._sem = semaphore
        self.provider_name = getattr(inner, "provider_name", "")
        self.model_name = getattr(inner, "model_name", "")
        self.model_info: ModelInfo | None = getattr(inner, "model_info", None)
        self.dimensions = getattr(inner, "dimensions", 0)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        async with self._sem:
            return await self._inner.embed_documents(texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        async with self._sem:
            return await self._inner.embed_queries(texts)

    async def ping(self) -> None:
        async with self._sem:
            await self._inner.ping()

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


class BoundedPublisher:
    """Rate-limited PublisherProvider proxy."""

    def __init__(self, inner: Any, semaphore: asyncio.Semaphore) -> None:
        self._inner = inner
        self._sem = semaphore
        self.provider_name = getattr(inner, "provider_name", "")

    async def list_posts(
        self, *, per_page=100, after_id=None, status="publish", fields=None
    ) -> list[WPPost]:
        async with self._sem:
            return await self._inner.list_posts(
                per_page=per_page, after_id=after_id, status=status, fields=fields
            )

    async def create_post(
        self, *, title, html, status, slug, meta=None, excerpt=""
    ) -> PublishResult:
        async with self._sem:
            return await self._inner.create_post(
                title=title, html=html, status=status, slug=slug, meta=meta, excerpt=excerpt
            )

    async def update_post(
        self, post_id, *, title=None, html=None, status=None, slug=None, meta=None, excerpt=None
    ) -> PublishResult:
        async with self._sem:
            return await self._inner.update_post(
                post_id=post_id,
                title=title,
                html=html,
                status=status,
                slug=slug,
                meta=meta,
                excerpt=excerpt,
            )

    async def get_post(self, post_id: int) -> WPPost | None:
        async with self._sem:
            return await self._inner.get_post(post_id)

    async def ping(self) -> None:
        async with self._sem:
            await self._inner.ping()

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
