"""Cohere Embed v4.0 adapter.

`POST {base}/embed` with input_type:
- `search_document` for indexed documents (embed_documents)
- `search_query` for retrieval queries (embed_queries)

Default model: embed-v4.0 (1024 dims). Credentials come from the project's
integrations — never hard-coded.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import ModelInfo, PermanentError
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "cohere"
DEFAULT_BASE = "https://api.cohere.com/v1"
DEFAULT_MODEL = "embed-v4.0"
DEFAULT_DIMENSIONS = 1024
BATCH_SIZE = 64


class CohereEmbedding(MetricMixin):
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
    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            results.extend(await self._embed_batch(batch, input_type))
        return results

    async def _embed_batch(self, batch: list[str], input_type: str) -> list[list[float]]:
        body = {
            "texts": batch,
            "model": self._model,
            "input_type": input_type,
            "embedding_types": ["float"],
        }
        retry_counter: list[int] = []

        async def _run() -> list[list[float]]:
            async def _call() -> httpx.Response:
                return await self._client.post("/embed", json=body)

            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="embedding.batch",
                logger_name="cohere_embed",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="embedding.batch")
            data = response.json()
            if not isinstance(data, dict) or "embeddings" not in data:
                raise PermanentError("embedding.batch: unexpected response shape")
            embeddings = data["embeddings"]
            if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                raise PermanentError(
                    "embedding.batch: provider returned fewer embeddings than requested"
                )
            return [[float(x) for x in vec] for vec in embeddings]

        return await self._observed(
            f"embedding.{input_type}", sum(len(t) for t in batch), _run, retry_counter
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "search_document")

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "search_query")

    async def ping(self) -> None:
        await self._embed(["ping"], "search_query")
