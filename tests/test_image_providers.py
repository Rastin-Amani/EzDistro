"""Image provider adapters — GeminiImage + FluxImage against httpx.MockTransport.

Covers: success, malformed response, 429/5xx (transient), auth failures,
content-policy refusals, usage metadata, BFL submit→poll→download flow,
dimension rounding, moderation failure, poll timeout, https-only download.
"""

from __future__ import annotations

import base64
import io

import httpx
import pytest
from PIL import Image

from app.providers.base import ImageRequest, PermanentError, TransientError
from app.providers.image.flux import FluxImage
from app.providers.image.gemini import GeminiImage


def _png(w: int = 64, h: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(w: int = 64, h: int = 32) -> str:
    return base64.b64encode(_png(w, h)).decode("ascii")


def _req(**kw) -> ImageRequest:
    kw.setdefault("prompt", "a dashboard warning light")
    return ImageRequest(**kw)


def _gemini_image_response() -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _png_b64()}}]}},
        ],
        "usageMetadata": {"promptTokenCount": 12, "totalTokenCount": 40},
    }


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
async def test_gemini_generate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/test-img:generateContent")
        assert request.url.params["key"] == "secret"
        assert b"dashboard" in request.read()
        return httpx.Response(200, json=_gemini_image_response())

    adapter = GeminiImage(
        model="test-img", api_key="secret", attempts=1, transport=httpx.MockTransport(handler)
    )
    result = await adapter.generate_image(_req())
    assert result.data.startswith(b"\x89PNG")
    assert result.mime_type == "image/png"
    assert result.provider == "gemini"
    assert result.model == "test-img"
    assert result.usage["prompt_tokens"] == 12
    await adapter.aclose()


async def test_gemini_negative_prompt_becomes_part():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json=_gemini_image_response())

    adapter = GeminiImage(
        model="m", api_key="k", attempts=1, transport=httpx.MockTransport(handler)
    )
    await adapter.generate_image(_req(negative_prompt="text, watermark"))
    assert b"Avoid in the image: text, watermark" in captured["body"]


async def test_gemini_malformed_response_is_permanent():
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": True})),
    )
    with pytest.raises(PermanentError):
        await adapter.generate_image(_req())


async def test_gemini_blocked_finish_reason_is_permanent():
    blocked = {
        "candidates": [{"content": {"parts": [{"text": "cannot"}]}, "finishReason": "SAFETY"}]
    }
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=blocked)),
    )
    with pytest.raises(PermanentError, match="SAFETY"):
        await adapter.generate_image(_req())


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_gemini_retryable_statuses(status: int):
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})),
    )
    with pytest.raises(TransientError):
        await adapter.generate_image(_req())


async def test_gemini_auth_error_is_permanent():
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})
        ),
    )
    with pytest.raises(PermanentError):
        await adapter.generate_image(_req())


async def test_gemini_ping_and_list_models():
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"models": [{"name": "models/m1"}, {"name": "models/m2"}]}
            )
        ),
    )
    await adapter.ping()
    models = await adapter.list_models()
    assert models == ["m1", "m2"]
    await adapter.aclose()


async def test_gemini_ping_auth_failure():
    adapter = GeminiImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})),
    )
    with pytest.raises(PermanentError):
        await adapter.ping()


# ---------------------------------------------------------------------------
# BFL / FLUX
# ---------------------------------------------------------------------------
async def test_flux_full_flow_success(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.bfl.test":
            return httpx.Response(200, content=_png(32, 16))
        assert request.headers["x-key"] == "fk"
        if request.url.path == "/v1/flux-2-klein":
            assert b'"prompt"' in request.read()
            return httpx.Response(200, json={"id": "task-1"})
        if request.url.path == "/v1/get_result":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"status": "Pending"})
            return httpx.Response(
                200, json={"status": "Ready", "result": {"sample": "https://cdn.bfl.test/img.png"}}
            )
        if request.url.host == "cdn.bfl.test":
            return httpx.Response(200, content=_png(32, 16))
        raise AssertionError(f"unexpected url {request.url}")

    adapter = FluxImage(
        model="flux-2-klein",
        api_key="fk",
        attempts=1,
        timeout=10,
        transport=httpx.MockTransport(handler),
    )
    result = await adapter.generate_image(_req(width=1600, height=896))
    assert result.data.startswith(b"\x89PNG")
    assert (result.width, result.height) == (1600, 896)
    assert result.usage["billed_megapixels"] == 2  # 1600×896 = 1.43MP → 2
    assert state["polls"] >= 2
    await adapter.aclose()


async def test_flux_dims_rounded_to_32(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    captured: dict = {}

    def combined(request: httpx.Request) -> httpx.Response:
        if request.url.host == "x.test":
            return httpx.Response(200, content=b"x")
        if request.url.path == "/v1/m":
            captured["body"] = request.read()
            return httpx.Response(200, json={"id": "t"})
        return httpx.Response(
            200, json={"status": "Ready", "result": {"sample": "https://x.test/i.png"}}
        )

    adapter = FluxImage(
        model="m", api_key="k", attempts=1, timeout=5, transport=httpx.MockTransport(combined)
    )
    result = await adapter.generate_image(_req(width=1290, height=725))
    assert (result.width, result.height) == (1280, 704)
    assert b"1280" in captured["body"] and b"704" in captured["body"]
    await adapter.aclose()


async def test_flux_moderation_is_permanent(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    adapter = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        timeout=5,
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"id": "t"})
                if r.url.path == "/v1/m"
                else httpx.Response(200, json={"status": "Content Moderation"})
            )
        ),
    )
    with pytest.raises(PermanentError, match="Moderation"):
        await adapter.generate_image(_req())


async def test_flux_submit_429_is_transient(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    adapter = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        timeout=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})),
    )
    with pytest.raises(TransientError):
        await adapter.generate_image(_req())


async def test_flux_poll_timeout_is_transient(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    adapter = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        timeout=0.15,
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"id": "t"})
                if r.url.path == "/v1/m"
                else httpx.Response(200, json={"status": "Pending"})
            )
        ),
    )
    with pytest.raises(TransientError, match="timed out"):
        await adapter.generate_image(_req())


async def test_flux_non_https_sample_refused(monkeypatch):
    monkeypatch.setattr("app.providers.image.flux.POLL_INTERVAL_SECONDS", 0)
    adapter = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        timeout=5,
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"id": "t"})
                if r.url.path == "/v1/m"
                else httpx.Response(
                    200,
                    json={"status": "Ready", "result": {"sample": "http://insecure.test/i.png"}},
                )
            )
        ),
    )
    with pytest.raises(PermanentError, match="https"):
        await adapter.generate_image(_req())


async def test_flux_submit_without_id_is_permanent():
    adapter = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        timeout=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with pytest.raises(PermanentError, match="task id"):
        await adapter.generate_image(_req())


async def test_flux_ping_auth_gate():
    ok = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={})),
    )
    await ok.ping()  # 400 = reachable + authenticated
    bad = FluxImage(
        model="m",
        api_key="k",
        attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})),
    )
    with pytest.raises(PermanentError):
        await bad.ping()
