"""OpenAI-compatible image adapter (`/images/generations`).

Works with OpenAI, AvalAI, OpenRouter-style gateways, and any server exposing
`POST {base}/images/generations` with Bearer auth. Set the integration's
base_url to the gateway (e.g. https://api.avalai.ir/v1) and the model to
whatever the gateway lists (e.g. gemini-3-pro-image).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.providers.base import ImageRequest, ImageResult, PermanentError, TransientError
from app.providers.http import (
    acquire_async_client,
    raise_for_provider,
    with_retry,
)
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "openai_compat"


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class OpenAICompatImage(MetricMixin):
    category = "image"
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "OpenAI-compatible /images/generations (OpenAI, AvalAI, any compatible gateway)",
        "supports_model_listing": True,
        "requires_api_key": True,
        "default_base_url": "https://api.openai.com/v1",
    }

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 180.0,
        attempts: int = 3,
        observer: Any = None,
        transport: Any = None,
    ) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._attempts = attempts
        self.timeout = timeout
        self._client = acquire_async_client(
            base_url, api_key=api_key, timeout=timeout, transport=transport
        )
        if observer is not None:
            self._set_observer(observer)
        self.model_name = model

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    async def generate_image(self, request: ImageRequest) -> ImageResult:
        prompt = request.prompt
        if request.negative_prompt:
            # OpenAI image API has no negative_prompt field; append as guidance.
            prompt = f"{prompt}\n\nAvoid in the image: {request.negative_prompt}"
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "size": f"{request.width}x{request.height}",
            "n": 1,
        }
        retry_counter: list[int] = []

        async def _run() -> ImageResult:
            import time

            started = time.monotonic()

            async def _call() -> httpx.Response:
                return await self._client.post("/images/generations", json=body)

            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="openai_image.generate",
                logger_name="image",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="openai_image.generate")
            data = response.json()
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                raise PermanentError("openai_image.generate: malformed response (no data)")
            first = items[0]
            b64 = first.get("b64_json")
            if b64:
                raw = base64.b64decode(str(b64))
            else:
                url = str(first.get("url") or "")
                if not url:
                    raise PermanentError(
                        "openai_image.generate: response has neither b64_json nor url"
                    )
                raw = await self._download(url)
            usage = data.get("usage") or {}
            return ImageResult(
                data=raw,
                mime_type=_sniff_mime(raw),
                provider=PROVIDER_NAME,
                model=self._model,
                usage={
                    k: int(usage.get(k) or 0)
                    for k in ("input_tokens", "output_tokens", "total_tokens")
                    if usage.get(k)
                },
                latency_ms=int((time.monotonic() - started) * 1000),
                retries=len(retry_counter),
            )

        return await self._observed(
            "image.generate", len(request.prompt), _run, retry_counter
        )

    async def _download(self, url: str) -> bytes:
        """Fetch a url-style result; https only (never a caller-controlled URL)."""
        if not url.startswith("https://"):
            raise PermanentError("openai_image.download: refusing non-https URL")

        async def _call() -> httpx.Response:
            return await self._client.get(url)

        response = await with_retry(
            _call, attempts=2, what="openai_image.download", logger_name="image"
        )
        if response.status_code >= 400:
            raise TransientError(f"openai_image.download: HTTP {response.status_code}")
        return response.content

    async def ping(self) -> None:
        """Health probe — GET /models (Bearer auth proves the key works)."""
        retry_counter: list[int] = []

        async def _run() -> None:
            async def _call() -> httpx.Response:
                return await self._client.get("/models")

            response = await with_retry(
                _call, attempts=1, what="openai_image.ping", logger_name="image"
            )
            raise_for_provider(response, what="openai_image.ping")

        await self._observed("image.ping", 0, _run, retry_counter)

    async def list_models(self) -> list[str]:
        """GET /models — gateway ids; image-capable models first when `mode` is exposed."""
        retry_counter: list[int] = []

        async def _run() -> list[str]:
            async def _call() -> httpx.Response:
                return await self._client.get("/models")

            response = await with_retry(
                _call, attempts=2, what="image.list_models", logger_name="image"
            )
            raise_for_provider(response, what="image.list_models")
            data = response.json()
            models = data.get("data") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return []
            ids: list[str] = []
            for m in models:
                if isinstance(m, dict) and m.get("id"):
                    if m.get("mode") and m["mode"] != "image_generation":
                        continue
                    ids.append(str(m["id"]))
            return sorted(ids)

        return await self._observed("image.list_models", 0, _run, retry_counter)
