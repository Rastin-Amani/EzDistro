"""Cohere-compatible reranker adapter (`POST {base}/rerank`).

Generic enough for Cohere, Jina and local reranker servers exposing the Cohere
rerank API shape. Returns RerankResult with relevance scores AND the original
documents (+ caller metadata), so downstream code never loses context.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import ModelInfo, PermanentError, RerankResult
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "cohere_compat"
DEFAULT_BASE = "https://api.cohere.com/v1"
DEFAULT_MODEL = "rerank-v4.0"


class CohereCompatReranker(MetricMixin):
    category = "reranker"
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        model: str = DEFAULT_MODEL,
        api_key: str = "",
        timeout: float = 60.0,
        attempts: int = 3,
        observer: Any = None,
        transport: Any = None,
    ) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._attempts = attempts
        self.timeout = timeout
        self._client = acquire_async_client(
            base_url, api_key=api_key, timeout=timeout, transport=transport
        )
        if observer is not None:
            self._set_observer(observer)
        self.model_info = ModelInfo(
            provider=PROVIDER_NAME,
            name=model,
            supports_streaming=False,
            supports_json=False,
        )
        self.model_name = model

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        body = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": max(1, top_n),
        }
        retry_counter: list[int] = []

        async def _run() -> list[RerankResult]:
            async def _call() -> httpx.Response:
                return await self._client.post("/rerank", json=body)

            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="rerank",
                logger_name="rerank",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="rerank")
            data = response.json()
            if not isinstance(data, dict) or "results" not in data:
                raise PermanentError("rerank: unexpected response shape")

            ranked: list[RerankResult] = []
            for item in data["results"]:
                if not isinstance(item, dict) or "index" not in item:
                    continue
                idx = int(item["index"])
                if idx < 0 or idx >= len(documents):
                    continue
                ranked.append(
                    RerankResult(
                        index=idx,
                        score=float(item.get("relevance_score", 0.0)),
                        document=documents[idx],
                        metadata=(metadata[idx] if metadata and idx < len(metadata) else None),
                    )
                )
            ranked.sort(key=lambda r: r.score, reverse=True)
            return ranked[:top_n]

        return await self._observed(
            "rerank", len(query) + sum(len(d) for d in documents), _run, retry_counter
        )

    async def ping(self) -> None:
        await self.rerank(query="ping", documents=["ping"], top_n=1)
