"""Qdrant vector store adapter — full VectorStoreProvider protocol.

Collection naming: `ezdistro_{project_slug}_{model_slug}` — namespaced per project
AND per embedding model, so switching models never corrupts an existing index.
Point ids are stable UUIDs derived from `{project_slug}:{wp_post_id}:{chunk_index}`
→ idempotent upserts (same id overwrites the vector, never duplicates).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models

from app.providers.base import SearchHit, VectorPoint

PROVIDER_NAME = "qdrant"


def project_key(project_slug: str, model: str) -> str:
    """Stable, URL-safe namespace key for a project+model index."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", project_slug.lower()).strip("-") or "project"
    model_part = re.sub(r"[^a-z0-9_-]+", "-", model.lower()).strip("-") or "model"
    return f"ezdistro-{slug}-{model_part}"[:63]


def point_id(project_slug: str, wp_post_id: int, chunk_index: int) -> str:
    return f"{project_slug}:{wp_post_id}:{chunk_index}"


def _to_filter(filters: dict[str, Any] | None) -> models.Filter | None:
    """Payload filter: exact match on each key (Qdrant `match value` semantics)."""
    if not filters:
        return None
    conditions = [
        models.FieldCondition(key=str(key), match=models.MatchValue(value=value))
        for key, value in filters.items()
        if value is not None
    ]
    return models.Filter(must=conditions) if conditions else None  # type: ignore[arg-type]


class QdrantStore:
    """Qdrant store bound to one project+model namespace."""

    category = "vector_store"
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        url: str,
        namespace: str,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._namespace = namespace
        self._api_key = api_key
        self._timeout = timeout
        self._client: AsyncQdrantClient | None = None

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(
                url=self._url, api_key=self._api_key or None, timeout=int(self._timeout)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- collection management ------------------------------------------------------
    async def ensure_collection(self, dimensions: int) -> None:
        client = await self._get_client()
        existing = await client.collection_exists(self._namespace)
        if existing:
            info = await client.get_collection(self._namespace)
            vectors = info.config.params.vectors
            size = (
                vectors.size
                if isinstance(vectors, models.VectorParams)
                else (vectors or {}).get("size")
            )
            if size != dimensions:
                raise ValueError(
                    f"Qdrant collection {self._namespace} exists with {size} dimensions but the "
                    f"embedding model produces {dimensions}. Changing embedding models requires a "
                    f"fresh namespace (different model slug)."
                )
            return
        await client.create_collection(
            collection_name=self._namespace,
            vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
        )

    async def delete_collection(self) -> None:
        client = await self._get_client()
        if await client.collection_exists(self._namespace):
            await client.delete_collection(self._namespace)

    # -- writes ----------------------------------------------------------------------
    async def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        client = await self._get_client()
        batch_size = 64
        for i in range(0, len(points), batch_size):
            chunk = points[i : i + batch_size]
            await client.upsert(
                collection_name=self._namespace,
                points=[
                    models.PointStruct(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, p.id).hex,
                        vector=p.vector,
                        payload=p.payload,
                    )
                    for p in chunk
                ],
            )

    async def delete(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        client = await self._get_client()
        client_delete = getattr(client, "delete", None)
        if client_delete is None:
            return
        await client_delete(
            collection_name=self._namespace,
            points_selector=[uuid.uuid5(uuid.NAMESPACE_URL, pid).hex for pid in point_ids],
        )

    async def update_payload(self, point_ids: list[str], payload: dict[str, Any]) -> None:
        """Update vector payloads without touching vectors (metadata-only change)."""
        if not point_ids or not payload:
            return
        client = await self._get_client()
        await client.set_payload(
            collection_name=self._namespace,
            payload=payload_safe(payload),
            points=[uuid.uuid5(uuid.NAMESPACE_URL, pid).hex for pid in point_ids],
        )

    async def delete_by_filter(self, filters: dict[str, Any]) -> None:
        client = await self._get_client()
        await client.delete(
            collection_name=self._namespace,
            points_selector=models.FilterSelector(
                filter=_to_filter(filters) or models.Filter(must=[])
            ),
        )

    # -- reads ------------------------------------------------------------------------
    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        threshold: float | None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        client = await self._get_client()
        score_threshold = threshold if threshold and threshold > 0 else None
        response = await client.query_points(
            collection_name=self._namespace,
            query=vector,
            limit=top_k,
            score_threshold=score_threshold,
            query_filter=_to_filter(filters),
        )
        hits: list[SearchHit] = []
        for r in response.points:
            payload = r.payload or {}
            hits.append(
                SearchHit(
                    id=str(r.id),
                    score=float(r.score),
                    payload={
                        k: v
                        for k, v in payload.items()
                        if isinstance(v, (str, int, float, bool, list, dict))
                    },
                )
            )
        return hits

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        client = await self._get_client()
        try:
            result = await client.count(
                collection_name=self._namespace,
                exact=True,
                count_filter=_to_filter(filters),
            )
            return int(result.count)
        except Exception:
            return 0

    async def scroll_ids(
        self, filters: dict[str, Any] | None = None, limit: int = 1000
    ) -> list[tuple[str, dict[str, Any]]]:
        """All (point_id, payload) matching the filter — used for stale cleanup."""
        client = await self._get_client()
        result: list[tuple[str, dict[str, Any]]] = []
        offset: Any = None
        while True:
            page = await client.scroll(
                collection_name=self._namespace,
                scroll_filter=_to_filter(filters),
                limit=min(limit, 1000),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in page[0]:
                payload = point.payload or {}
                result.append(
                    (
                        str(point.id),
                        {
                            k: v
                            for k, v in payload.items()
                            if isinstance(v, (str, int, float, bool, list, dict))
                        },
                    )
                )
            offset = page[1]
            if offset is None:
                break
            if len(result) >= limit:
                break
        return result

    async def ping(self) -> None:
        """Health probe — Qdrant exposes GET /healthz (200 = ready)."""
        from app.providers.base import TransientError

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self._url.rstrip('/')}/healthz")
            if response.status_code != 200:
                raise TransientError(f"qdrant ping failed with HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            raise TransientError(f"qdrant ping failed: {exc}") from exc


def payload_safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Payloads must be JSON-serializable — Qdrant rejects non-primitives."""
    return {
        k: v
        for k, v in payload.items()
        if isinstance(v, (str, int, float, bool, list, dict)) and v is not None
    }
