"""Google Gemini image adapter (native generateContent, responseModalities IMAGE).

Model ids are data (e.g. gemini-3-pro-image); dimensions come back encoded in
the image bytes — the caller's quality gate decodes actuals with Pillow.
"""

from __future__ import annotations

import base64
import contextlib
from typing import Any

import httpx

from app.providers.base import (
    ImageRequest,
    ImageResult,
    ModelInfo,
    PermanentError,
)
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "gemini"
DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiImage(MetricMixin):
    category = "image"
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "Google Gemini image generation (generateContent + responseModalities IMAGE)",
        "requires_api_key": True,
        "supports_model_listing": True,
        "default_base_url": DEFAULT_BASE,
    }

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        model: str,
        api_key: str,
        timeout: float = 180.0,
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
            base_url, api_key="", timeout=timeout, transport=transport
        )
        if observer is not None:
            self._set_observer(observer)
        self.model_info = ModelInfo(provider=PROVIDER_NAME, name=model)
        self.model_name = model

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    def _params(self) -> dict[str, str]:
        return {"key": self._api_key} if self._api_key else {}

    async def generate_image(self, request: ImageRequest) -> ImageResult:
        parts: list[dict[str, Any]] = [{"text": request.prompt}]
        if request.negative_prompt:
            parts.append({"text": f"Avoid in the image: {request.negative_prompt}."})
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": request.aspect_ratio},
            },
        }
        retry_counter: list[int] = []

        async def _call() -> httpx.Response:
            return await self._client.post(
                f"/models/{self._model}:generateContent", params=self._params(), json=body
            )

        async def _run() -> ImageResult:
            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="gemini_image.generate",
                logger_name="gemini_image",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="gemini_image.generate")
            data = response.json()
            return self._parse_response(data, len(retry_counter))

        return await self._observed("image.generate", len(request.prompt), _run, retry_counter)

    def _parse_response(self, data: Any, retries: int) -> ImageResult:
        """Extract the first inline image part; a refusal is a permanent error."""
        if not isinstance(data, dict):
            raise PermanentError("gemini_image: unexpected response shape")
        try:
            candidates = data.get("candidates") or []
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentError("gemini_image: malformed candidates") from exc
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
                raw = base64.b64decode(inline["data"])
                usage = data.get("usageMetadata") or {}
                return ImageResult(
                    data=raw,
                    mime_type=mime,
                    provider=PROVIDER_NAME,
                    model=self._model,
                    usage={
                        "prompt_tokens": int(usage.get("promptTokenCount") or 0),
                        "total_tokens": int(usage.get("totalTokenCount") or 0),
                    },
                    retries=retries,
                )
        # No image part: surface the finish reason / block reason.
        reason = ""
        with contextlib.suppress(KeyError, IndexError, TypeError):
            reason = str(data["candidates"][0].get("finishReason") or "")
        if not reason:
            reason = str((data.get("promptFeedback") or {}).get("blockReason") or "")
        raise PermanentError(f"gemini_image: no image in response (reason: {reason or 'none'})")

    async def ping(self) -> None:
        retry_counter: list[int] = []

        async def _call() -> httpx.Response:
            return await self._client.get("/models", params=self._params())

        async def _run() -> None:
            response = await with_retry(
                _call,
                attempts=2,
                what="gemini_image.ping",
                logger_name="gemini_image",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="gemini_image.ping")

        await self._observed("image.ping", 0, _run, retry_counter)

    async def list_models(self) -> list[str]:
        retry_counter: list[int] = []

        async def _call() -> httpx.Response:
            return await self._client.get("/models", params=self._params())

        async def _run() -> list[str]:
            response = await with_retry(
                _call,
                attempts=2,
                what="gemini_image.list_models",
                logger_name="gemini_image",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="gemini_image.list_models")
            data = response.json()
            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return []
            return sorted(
                str(m.get("name", "")).removeprefix("models/")
                for m in models
                if isinstance(m, dict)
            )

        return await self._observed("image.list_models", 0, _run, retry_counter)
