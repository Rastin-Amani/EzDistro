"""Fake providers + fake registry for pipeline tests (no network).

Implements the provider protocols from app/providers/base.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.providers.base import (
    ImageRequest,
    ImageResult,
    LLMResult,
    ModelInfo,
    PermanentError,
    PublishResult,
    RerankResult,
    SearchHit,
    VectorPoint,
    WPPost,
)


class FakeLLM:
    def __init__(self, responses: list[str], delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.provider_name = "fake"
        self.model_name = "fake-model"
        self.model_info = ModelInfo(provider="fake", name="fake-model")
        self.timeout = 30.0
        self.delay = delay  # simulated provider latency
        self.active = 0  # current in-flight calls
        self.max_active = 0  # peak observed concurrency

    async def _throttle(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self.active -= 1

    def _next(self, json_mode: bool, user: str = "") -> str:
        self.calls.append({"json_mode": json_mode, "user": user})
        if not self.responses:
            raise AssertionError("FakeLLM ran out of canned responses")
        return self.responses.pop(0)

    def _set_observer(self, observer) -> None:
        self._observer = observer

    def _emit(self, operation: str, latency_ms: int, usage: dict | None = None) -> None:
        from app.providers.metrics import NoopObserver, ProviderCallRecord

        observer = getattr(self, "_observer", None) or NoopObserver()
        usage = usage or {}
        with _suppress():
            observer.on_call(
                ProviderCallRecord(
                    provider=self.provider_name,
                    model=self.model_name,
                    operation=operation,
                    latency_ms=latency_ms,
                    success=True,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
            )

    async def generate(self, *, system=None, user, params=None) -> LLMResult:
        await self._throttle()
        text = self._next(json_mode=False, user=user)
        self._emit("llm.generate", 42, {"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResult(
            text=text,
            provider=self.provider_name,
            model=self.model_name,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            latency_ms=42,
        )

    async def generate_json(self, *, system=None, user, params=None) -> dict[str, Any]:
        await self._throttle()
        import json as _json

        text = self._next(json_mode=True, user=user)
        self._emit("llm.generate_json", 42)
        return _json.loads(text)

    async def stream(self, *, system=None, user, params=None):
        text = self._next(json_mode=False)
        for token in text.split(" "):
            yield token + " "

    async def ping(self) -> None:
        self._next(json_mode=False)


class FakeEmbedding:
    dimensions: int = 8

    def __init__(self, delay: float = 0.0) -> None:
        self.requests: list[tuple[str, list[str]]] = []
        self.provider_name = "fake"
        self.model_name = "fake-embed"
        self.model_info = ModelInfo(provider="fake", name="fake-embed", dimensions=8)
        self.delay = delay  # simulated provider latency (async layer)

    def _embed(self, texts: list[str], kind: str) -> list[list[float]]:
        import hashlib

        self.requests.append((kind, list(texts)))
        # deterministic per-process hash (builtin hash() is randomized)
        return [
            [
                float(int(hashlib.sha256(t.encode("utf-8")).hexdigest()[:8], 16) % 1000) / 1000.0
                for _ in range(self.dimensions)
            ]
            for t in texts
        ]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._embed(texts, "documents")

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._embed(texts, "queries")

    async def ping(self) -> None:
        await self.embed_queries(["ping"])


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[str, VectorPoint] = {}
        self.ensure_calls: list[int] = []
        self.upsert_calls: list[list[VectorPoint]] = []
        self.deleted_ids: list[str] = []
        self.namespace = "seoz-test-model"
        self.provider_name = "fake"

    async def ensure_collection(self, dimensions: int) -> None:
        self.ensure_calls.append(dimensions)

    async def delete_collection(self) -> None:
        self.points.clear()

    async def upsert(self, points: list[VectorPoint]) -> None:
        self.upsert_calls.append(list(points))
        for p in points:
            self.points[p.id] = p

    async def delete(self, point_ids: list[str]) -> None:
        self.deleted_ids.extend(point_ids)
        for pid in point_ids:
            self.points.pop(pid, None)

    async def update_payload(self, point_ids: list[str], payload: dict) -> None:
        for pid in point_ids:
            point = self.points.get(pid)
            if point is not None:
                point.payload.update(payload)

    async def scroll_ids(self, filters=None, limit: int = 1000) -> list[tuple[str, dict]]:
        out = []
        for pid, point in self.points.items():
            if filters and any(point.payload.get(k) != v for k, v in filters.items()):
                continue
            out.append((pid, dict(point.payload)))
            if len(out) >= limit:
                break
        return out

    async def query(self, vector, *, top_k, threshold, filters=None) -> list[SearchHit]:
        hits = []
        for p in self.points.values():
            if filters:
                for key, value in filters.items():
                    if p.payload.get(key) != value:
                        break
                else:
                    pass
                if any(p.payload.get(key) != value for key, value in filters.items()):
                    continue
            score = 1.0 / (1.0 + sum(abs(a - b) for a, b in zip(vector, p.vector, strict=False)))
            if threshold is None or score >= threshold:
                hits.append(SearchHit(id=p.id, score=score, payload=dict(p.payload)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    async def count(self, filters=None) -> int:
        if not filters:
            return len(self.points)
        return sum(
            1
            for p in self.points.values()
            if all(p.payload.get(k) == v for k, v in filters.items())
        )

    async def ping(self) -> None:
        pass


class FakeReranker:
    def __init__(self) -> None:
        self.calls = 0
        self.provider_name = "fake"
        self.model_name = "fake-rerank"
        self.model_info = ModelInfo(provider="fake", name="fake-rerank")

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[RerankResult]:
        self.calls += 1
        results = [
            RerankResult(
                index=i,
                score=float(len(documents) - i),
                document=documents[i],
                metadata=(metadata[i] if metadata else None),
            )
            for i in range(min(top_n, len(documents)))
        ]
        return results

    async def ping(self) -> None:
        await self.rerank(query="ping", documents=["ping"], top_n=1)


class FakeImageProvider:
    """Scriptable image provider: queue ImageResult/exceptions, else auto PNG."""

    category = "image"
    model_name = "fake-image-1"
    model_info = ModelInfo(provider="fakeimg", name="fake-image-1")
    timeout = 30.0

    def __init__(self, provider_name: str = "fakeimg") -> None:
        self.provider_name = provider_name
        self.calls: list[ImageRequest] = []
        self.results: list[Any] = []  # ImageResult | Exception (scripted, FIFO)
        self.ping_calls = 0

    def queue(self, *items: Any) -> FakeImageProvider:
        self.results.extend(items)
        return self

    def _render_png(self, width: int, height: int) -> bytes:
        import io
        import random

        from PIL import Image

        # noise, not flat colour: realistic PNG size (> quality-gate minimum)
        rng = random.Random(42)
        w, h = max(1, int(width)), max(1, int(height))
        img = Image.frombytes("RGB", (w, h), bytes(rng.randrange(256) for _ in range(w * h * 3)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def generate_image(self, request: ImageRequest) -> ImageResult:
        self.calls.append(request)
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ImageResult(
            data=self._render_png(request.width, request.height),
            mime_type="image/png",
            width=request.width,
            height=request.height,
            provider=self.provider_name,
            model=self.model_name,
            usage={},
            latency_ms=55,
        )

    async def ping(self) -> None:
        self.ping_calls += 1


class FakePublisher:
    def __init__(self, posts: list[WPPost] | None = None) -> None:
        self.posts = posts or []
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.next_id = 1000
        self.provider_name = "fake"
        # meta stored per created post, mirroring WP's registered meta behavior
        self.post_meta: dict[int, dict[str, str]] = {}
        # optional scripted failure for crash simulation
        self.crash_after_create: bool = False
        # media library (upload_media / set_featured_media)
        self.media: list[dict[str, Any]] = []
        self.next_media_id = 5000
        self.featured_calls: list[tuple[int, int]] = []
        self.upload_media_error: Exception | None = None

    async def list_posts(
        self, *, per_page=100, after_id=None, status="publish", fields=None
    ) -> list[WPPost]:
        results = [p for p in self.posts if after_id is None or p.id > after_id]
        return sorted(results, key=lambda p: p.id)[:per_page]

    async def create_post(
        self, *, title, html, status, slug, meta=None, excerpt=""
    ) -> PublishResult:
        self.next_id += 1
        self.created.append(
            {
                "id": self.next_id,
                "title": title,
                "html": html,
                "status": status,
                "slug": slug,
                "meta": meta,
                "excerpt": excerpt,
            }
        )
        if meta:
            self.post_meta.setdefault(self.next_id, {}).update(meta)
        if self.crash_after_create:
            # simulate worker death right after WP accepted the post:
            # the post id never reaches the database
            raise SystemExit(1)
        return PublishResult(post_id=self.next_id, link=f"https://site.test/?p={self.next_id}")

    async def find_post_by_slug(self, slug: str) -> WPPost | None:
        for created in self.created:
            if created.get("slug") == slug:
                return WPPost(
                    id=created["id"],
                    title="orphan",
                    content_html="",
                    link=f"https://site.test/?p={created['id']}",
                    status="publish",
                    modified="",
                )
        return None

    async def update_post(
        self, post_id, *, title=None, html=None, status=None, slug=None, meta=None, excerpt=None
    ) -> PublishResult:
        self.updated.append({"post_id": post_id, "title": title, "html": html, "status": status})
        return PublishResult(post_id=post_id, link=f"https://site.test/?p={post_id}")

    async def unpublish_post(self, post_id: int) -> PublishResult:
        self.updated.append({"post_id": post_id, "status": "private"})
        return PublishResult(
            post_id=post_id, link=f"https://site.test/?p={post_id}", status_code=200
        )

    async def get_post(self, post_id: int) -> WPPost | None:
        for p in self.posts:
            if p.id == post_id:
                return p
        return None

    async def ping(self) -> None:
        pass

    async def upload_media(
        self,
        *,
        data: bytes,
        filename: str,
        title: str = "",
        alt_text: str = "",
        caption: str = "",
        post_id: int | None = None,
    ) -> dict[str, Any]:
        if self.upload_media_error is not None:
            raise self.upload_media_error
        self.next_media_id += 1
        media_id = self.next_media_id
        record = {
            "id": media_id,
            "filename": filename,
            "title": title,
            "alt_text": alt_text,
            "caption": caption,
            "post_id": post_id,
            "size": len(data),
        }
        self.media.append(record)
        return {
            "id": media_id,
            "url": f"https://site.test/wp-content/uploads/{filename}",
            "mime_type": "image/webp",
        }

    async def set_featured_media(self, post_id: int, media_id: int) -> PublishResult:
        self.featured_calls.append((post_id, media_id))
        return PublishResult(post_id=post_id, link=f"https://site.test/?p={post_id}")

    def media_uploads_of(self, filename_substring: str) -> list[dict[str, Any]]:
        return [m for m in self.media if filename_substring in m["filename"]]


class FakeRegistry:
    """Returns fakes for every provider; replace attributes to customize."""

    def __init__(self) -> None:
        self.llm = FakeLLM([])
        self.embedding = FakeEmbedding()
        self.vector = FakeVectorStore()
        self.reranker: FakeReranker | None = FakeReranker()
        self.publisher = FakePublisher()
        self.image = FakeImageProvider()
        self.images: dict[str, FakeImageProvider] = {"primary": self.image}

    def get_llm_provider(
        self, project, settings, observer=None, integration=None, role="outline", role_config=None
    ) -> FakeLLM:
        return self.llm

    def get_embedding_provider(
        self, project, settings, observer=None, integration=None
    ) -> FakeEmbedding:
        return self.embedding

    def get_vector_provider(
        self, project, settings, observer=None, integration=None
    ) -> FakeVectorStore:
        return self.vector

    def get_reranker_provider(
        self, project, settings, observer=None, integration=None
    ) -> FakeReranker | None:
        return self.reranker

    def get_publisher_provider(
        self, project, settings, observer=None, integration=None
    ) -> FakePublisher:
        return self.publisher

    def get_image_provider(
        self, project, settings, observer=None, integration=None, role_config=None
    ) -> FakeImageProvider:
        name = str((role_config or {}).get("provider") or "primary")
        if name not in self.images:
            self.images[name] = FakeImageProvider(provider_name=name)
        return self.images[name]


class _suppress:
    """tiny context manager to keep fake emission side-effect free"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
