"""LLM adapter tests with mocked HTTP transport (OpenAI-compat + Gemini)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import GenerationParams, PermanentError, TransientError
from app.providers.embedding.openai_compat import OpenAICompatEmbedding
from app.providers.http import raise_for_provider, validate_llm_json_response
from app.providers.llm.gemini import GeminiLLM
from app.providers.llm.openai_compat import OpenAICompatLLM
from app.providers.metrics import ProviderCallRecord
from app.providers.publish.wordpress import WordPressPublisher
from app.providers.rerank.cohere_compat import CohereCompatReranker


class RecordingObserver:
    def __init__(self) -> None:
        self.records: list[ProviderCallRecord] = []

    def on_call(self, record: ProviderCallRecord) -> None:
        self.records.append(record)


def transport_for(handler):
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Error classification (raise_for_provider)
# ---------------------------------------------------------------------------
def test_html_response_is_flagged_as_web_page_not_api():
    """A 404 serving the provider's dashboard HTML (wrong base URL path) must
    produce a helpful message instead of dumping raw HTML."""
    response = httpx.Response(
        404,
        text="<!DOCTYPE html><html><head><meta charSet='utf-8'/><link rel='preload' href='/_next/static/…'/></head></html>",
    )
    with pytest.raises(PermanentError) as exc:
        raise_for_provider(response, what="llm.ping")
    message = str(exc.value)
    assert "HTML web page" in message
    assert "base URL" in message
    assert "<html" not in message


def test_pooled_client_survives_a_job_longer_than_the_idle_ttl():
    """A research/writing job holds ONE pooled client for many sequential calls.
    The sweeper used to close it after IDLE_TTL mid-run, killing every later
    call with 'Cannot send a request, as the client has been closed'."""
    import asyncio

    from app.providers import http as H

    H._CLIENT_POOL.clear()
    try:
        client = H.acquire_async_client("https://api.example.com/v1", api_key="k", timeout=5)
        again = H.acquire_async_client("https://api.example.com/v1", api_key="k", timeout=5)
        assert again is client  # pooled, not rebuilt

        # httpx awaits every request hook, so the hook must be awaitable
        key = next(iter(H._CLIENT_POOL))
        H._CLIENT_POOL[key] = (0.0, client, 5)
        hook = client.event_hooks["request"][-1]
        asyncio.run(hook(httpx.Request("POST", "https://api.example.com/v1")))
        touched = H._CLIENT_POOL[key][0]
        assert touched > H.IDLE_TTL  # stamp refreshed to "now"
        assert H.sweep_idle_clients(now=touched + 1) == 0

        # a client whose own request may still be in flight is never closed
        H._CLIENT_POOL[key] = (touched - 200, client, 600)
        assert H.sweep_idle_clients(now=touched) == 0
        H._CLIENT_POOL[key] = (touched - 700, client, 600)
        assert H.sweep_idle_clients(now=touched) == 1
    finally:
        H._CLIENT_POOL.clear()


def test_empty_content_from_a_reasoning_model_names_the_cause():
    """A reasoning model that spent its whole budget returns empty content with
    finish_reason=length — the error must say so, not just 'empty content'."""
    reasoning = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": "x" * 100},
                "finish_reason": "length",
            }
        ]
    }
    with pytest.raises(PermanentError) as excinfo:
        validate_llm_json_response(reasoning, "llm.generate")
    assert "max_tokens" in str(excinfo.value)

    # A genuinely empty answer (model stopped) keeps the plain message.
    stopped = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    with pytest.raises(PermanentError) as excinfo:
        validate_llm_json_response(stopped, "llm.generate")
    assert "empty completion content" in str(excinfo.value)


def test_json_error_response_keeps_api_body():
    response = httpx.Response(
        401, json={"error": {"message": "invalid api key", "code": "invalid_api_key"}}
    )
    with pytest.raises(PermanentError) as exc:
        raise_for_provider(response, what="llm.ping")
    assert "invalid api key" in str(exc.value)


# ---------------------------------------------------------------------------
# OpenAI-compatible LLM
# ---------------------------------------------------------------------------
def openai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["model"] == "gpt-test"
    assert body["messages"][-1]["role"] == "user"
    if body.get("response_format") == {"type": "json_object"}:
        content = '{"title": "ok"}'
    elif body.get("stream"):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            b"data: [DONE]\n\n",
        )
    else:
        content = "hello world"
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        },
    )


def test_openai_generate_and_metrics():
    observer = RecordingObserver()
    llm = OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        model="gpt-test",
        api_key="sk-test",
        transport=transport_for(openai_handler),
        observer=observer,
    )
    result = pytest.mark.asyncio(lambda: None)  # placeholder no-op
    import asyncio

    result = asyncio.run(
        llm.generate(system="sys", user="hello", params=GenerationParams(max_tokens=100))
    )
    assert result.text == "hello world"
    assert result.provider == "openai_compat"
    assert result.model == "gpt-test"
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 7}
    assert result.latency_ms >= 0

    assert len(observer.records) == 1
    record = observer.records[0]
    assert record.success is True
    assert record.operation == "llm.generate"
    assert record.prompt_tokens == 12
    assert record.completion_tokens == 7
    assert record.request_chars == 5  # "hello"
    asyncio.run(llm.aclose())


def test_openai_generate_json():
    import asyncio

    llm = OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        model="gpt-test",
        api_key="sk-test",
        transport=transport_for(openai_handler),
    )
    result = asyncio.run(llm.generate_json(system=None, user="make json"))
    assert result == {"title": "ok"}
    asyncio.run(llm.aclose())


def test_openai_stream():
    import asyncio

    llm = OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        model="gpt-test",
        api_key="sk-test",
        transport=transport_for(openai_handler),
    )

    async def collect():
        return "".join([chunk async for chunk in llm.stream(system=None, user="stream")])

    assert asyncio.run(collect()) == "hello"
    asyncio.run(llm.aclose())


def test_openai_error_classification():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    llm = OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        model="gpt-test",
        api_key="sk-test",
        attempts=1,  # no retries in test
        transport=transport_for(handler),
    )
    with pytest.raises(TransientError):
        asyncio.run(llm.generate(system=None, user="x"))
    asyncio.run(llm.aclose())


# ---------------------------------------------------------------------------
# Gemini LLM
# ---------------------------------------------------------------------------
def gemini_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert "key=test-key" in str(request.url)
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["generationConfig"]["maxOutputTokens"] == 100
    json_mode = body["generationConfig"].get("responseMimeType") == "application/json"
    content = '{"title": "gem"}' if json_mode else "Gemini response"
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": content}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 9},
        },
    )


def test_gemini_generate_and_json():
    import asyncio

    llm = GeminiLLM(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.0-flash",
        api_key="test-key",
        transport=transport_for(gemini_handler),
    )
    result = asyncio.run(
        llm.generate(system="sys", user="hi", params=GenerationParams(max_tokens=100))
    )
    assert result.text == "Gemini response"
    assert result.provider == "gemini"
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 9}

    parsed = asyncio.run(
        llm.generate_json(system="sys", user="json", params=GenerationParams(max_tokens=100))
    )
    assert parsed == {"title": "gem"}
    asyncio.run(llm.aclose())


def test_gemini_garbage_json_raises_value_error():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
        )

    llm = GeminiLLM(base_url="https://x", model="m", api_key="k", transport=transport_for(handler))
    with pytest.raises(ValueError):
        asyncio.run(llm.generate_json(system=None, user="json"))
    asyncio.run(llm.aclose())


def test_openai_compat_embedding():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"])
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [i * 0.1] * 2} for i in range(n)]}
        )

    emb = OpenAICompatEmbedding(
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        dimensions=2,
        api_key="k",
        transport=transport_for(handler),
    )
    vectors = asyncio.run(emb.embed_documents(["a", "b"]))
    assert len(vectors) == 2
    assert vectors[1] == [0.1, 0.1]
    asyncio.run(emb.aclose())


# ---------------------------------------------------------------------------
# Reranker — scores + metadata preservation
# ---------------------------------------------------------------------------
def test_reranker_preserves_scores_and_metadata():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "rerank-test"
        n = len(body["documents"])
        return httpx.Response(
            200,
            json={"results": [{"index": i, "relevance_score": (n - i) / n} for i in range(n)]},
        )

    reranker = CohereCompatReranker(
        base_url="https://api.cohere.com/v1",
        model="rerank-test",
        api_key="k",
        transport=transport_for(handler),
    )
    docs = [
        "first text",
        "second text",
        "third text",
    ]
    meta = [{"url": "/1"}, {"url": "/2"}, {"url": "/3"}]
    results = asyncio.run(reranker.rerank(query="seo", documents=docs, top_n=2, metadata=meta))

    assert len(results) == 2
    assert results[0].index == 0
    assert results[0].score > results[1].score
    assert results[0].document == "first text"  # original text preserved
    assert results[0].metadata == {"url": "/1"}  # caller metadata preserved
    asyncio.run(reranker.aclose())


# ---------------------------------------------------------------------------
# Publisher — create / update / get
# ---------------------------------------------------------------------------
def test_publisher_create_update_get():
    import asyncio

    posts = {
        "1001": {
            "id": 1001,
            "title": {"raw": "t"},
            "content": {"raw": "<p>c</p>"},
            "link": "https://s.test/?p=1001",
            "status": "draft",
            "modified": "2026-01-01",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/wp-json/wp/v2/posts":
            body = json.loads(request.content)
            pid = 1001
            posts[str(pid)] = {
                "id": pid,
                "title": {"raw": body["title"]},
                "content": {"raw": body["content"]},
                "link": f"https://s.test/?p={pid}",
                "status": body["status"],
                "modified": "2026-01-01",
            }
            return httpx.Response(201, json=posts[str(pid)])
        if request.method == "POST" and "/posts/" in path:
            pid = int(path.rsplit("/", 1)[-1])
            body = json.loads(request.content)
            posts[str(pid)].update(body)
            return httpx.Response(200, json=posts[str(pid)])
        if request.method == "GET" and "/posts/" in path:
            pid = int(path.rsplit("/", 1)[-1])
            if str(pid) not in posts:
                return httpx.Response(404, json={"code": "rest_post_invalid_id"})
            return httpx.Response(200, json=posts[str(pid)])
        if request.method == "GET" and path == "/wp-json":
            return httpx.Response(200, json={"name": "WP"})
        return httpx.Response(404)

    pub = WordPressPublisher(
        base_url="https://s.test",
        username="u",
        password="app-pass",
        transport=transport_for(handler),
    )
    created = asyncio.run(
        pub.create_post(title="Title", html="<p>x</p>", status="draft", slug="onvan")
    )
    assert created.post_id == 1001

    updated = asyncio.run(pub.update_post(1001, status="publish", title="New title"))
    assert updated.link == "https://s.test/?p=1001"

    fetched = asyncio.run(pub.get_post(1001))
    assert fetched is not None
    assert fetched.title == "New title"
    assert fetched.status == "publish"

    asyncio.run(pub.ping())  # /wp-json probe
    assert asyncio.run(pub.get_post(9999)) is None
    asyncio.run(pub.aclose())
