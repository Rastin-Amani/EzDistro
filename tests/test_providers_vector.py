"""Qdrant vector store tests (mocked AsyncQdrantClient)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.base import VectorPoint
from app.providers.vector.qdrant_store import QdrantStore, payload_safe, point_id, project_key


def make_store(**kwargs) -> QdrantStore:
    return QdrantStore(url="http://qdrant.test:6333", namespace="seoz-proj-model", **kwargs)


def test_project_key_namespacing():
    assert project_key("My Project", "embed-v4.0") == "seoz-my-project-embed-v4-0"
    assert project_key("پروژه", "مدل") == "seoz-project-model"
    assert len(project_key("a" * 100, "b" * 100)) <= 63


def test_point_id_stable():
    assert point_id("proj", 42, 0) == "proj:42:0"
    assert point_id("proj", 42, 0) == point_id("proj", 42, 0)


def test_payload_safe_filters_non_primitives():
    assert payload_safe({"a": 1, "b": "x", "c": None, "d": [1, 2]}) == {
        "a": 1,
        "b": "x",
        "d": [1, 2],
    }


def test_ensure_collection_creates_when_missing():
    store = make_store()
    client = AsyncMock()
    client.collection_exists.return_value = False
    store._client = client

    asyncio.run(store.ensure_collection(1024))
    client.create_collection.assert_awaited_once()
    args = client.create_collection.await_args.kwargs
    assert args["vectors_config"].size == 1024


def test_ensure_collection_detects_dimension_mismatch():
    store = make_store()
    client = AsyncMock()
    client.collection_exists.return_value = True
    info = MagicMock()
    info.config.params.vectors.size = 1536
    client.get_collection.return_value = info
    store._client = client

    with pytest.raises(ValueError, match="dimensions"):
        asyncio.run(store.ensure_collection(1024))


def test_upsert_and_delete_and_query():
    store = make_store()
    client = AsyncMock()
    # query_points returns ScoredPoint-like objects
    hit = MagicMock()
    hit.id = "abc"
    hit.score = 0.9
    hit.payload = {"title": "t", "project": "seoz-proj-model"}
    response = MagicMock()
    response.points = [hit]
    client.query_points.return_value = response
    store._client = client

    points = [VectorPoint(id="proj:1:0", vector=[0.1] * 4, payload={"project": "seoz-proj-model"})]
    asyncio.run(store.upsert(points))
    assert client.upsert.await_count == 1
    sent = client.upsert.await_args.kwargs["points"]
    assert len(sent) == 1

    results = asyncio.run(
        store.query([0.1] * 4, top_k=5, threshold=0.5, filters={"project": "seoz-proj-model"})
    )
    assert len(results) == 1
    assert results[0].score == 0.9
    # filter was forwarded
    assert client.query_points.await_args.kwargs["query_filter"] is not None

    asyncio.run(store.delete(["proj:1:0"]))
    assert client.delete.await_count == 1

    asyncio.run(store.delete_collection())
    client.delete_collection.assert_awaited_once()


def test_count_uses_filter():
    store = make_store()
    client = AsyncMock()
    count = MagicMock()
    count.count = 7
    client.count.return_value = count
    store._client = client

    assert asyncio.run(store.count(filters={"project": "seoz-proj-model"})) == 7
    assert client.count.await_args.kwargs["count_filter"] is not None


def test_ping_checks_healthz():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200)

    store = QdrantStore(url="http://qdrant.test:6333", namespace="ns")
    # ping uses its own httpx client against the store URL — inject a mock transport
    import app.providers.vector.qdrant_store as qs

    original = qs.httpx.AsyncClient
    try:
        qs.httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(handler), **kw)  # type: ignore[assignment]
        asyncio.run(store.ping())
    finally:
        qs.httpx.AsyncClient = original
