"""OpenAI-compatible embedding adapter (`POST {base}/embeddings`).

Batches internally (never one request per text) and implements the full
EmbeddingProvider protocol. Note: OpenAI embeddings do not distinguish
documents from queries — both methods hit the same endpoint.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import ModelInfo, PermanentError
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "openai_compat"
DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536
BATCH_SIZE = 32


class OpenAICompatEmbedding(MetricMixin):
    category = "embedding"
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        api_key: str = "",
        timeout: float = 120.0,
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
        self.dimensions = int(dimensions)
        self._client = acquire_async_client(
            base_url, api_key=api_key, timeout=timeout, transport=transport
        )
        if observer is not None:
            self._set_observer(observer)
        self.model_info = ModelInfo(
            provider=PROVIDER_NAME,
            name=model,
            dimensions=self.dimensions,
            supports_streaming=False,
            supports_json=False,
        )
        self.model_name = model

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    # ------------------------------------------------------------------ core
    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            results.extend(await self._embed_batch(batch))
        return results

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self._model, "input": batch}
        retry_counter: list[int] = []

        async def _run() -> list[list[float]]:
            async def _call() -> httpx.Response:
                return await self._client.post("/embeddings", json=body)

            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="embedding.batch",
                logger_name="embedding",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="embedding.batch")
            data = response.json()
            if not isinstance(data, dict) or "data" not in data:
                raise PermanentError("embedding.batch: unexpected response shape")

            indexed: dict[int, list[float]] = {}
            for item in data["data"]:
                if not isinstance(item, dict) or "index" not in item or "embedding" not in item:
                    raise PermanentError("embedding.batch: malformed item")
                indexed[int(item["index"])] = [float(x) for x in item["embedding"]]

            vectors = [indexed.get(i) for i in range(len(batch))]
            if any(v is None for v in vectors):
                raise PermanentError(
                    "embedding.batch: provider returned fewer embeddings than requested"
                )
            return [v for v in vectors if v is not None]

        return await self._observed(
            "embedding.batch", sum(len(t) for t in batch), _run, retry_counter
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def ping(self) -> None:
        await self._embed(["ping"])
