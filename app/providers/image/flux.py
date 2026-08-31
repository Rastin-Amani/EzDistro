"""Black Forest Labs FLUX adapter (async submit + poll REST API).

Flow: POST {base}/v1/{model} → {"id"} → poll GET /v1/get_result?id=… (x-key
header) until status "Ready" → download the sample URL (https only). Width and
height are rounded to /32 (BFL constraint). Megapixel-rounded billing is
reflected in result.usage so the cost estimator never re-derives it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.providers.base import (
    ImageRequest,
    ImageResult,
    ModelInfo,
    PermanentError,
    TransientError,
)
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "bfl"
DEFAULT_BASE = "https://api.bfl.ai"
POLL_INTERVAL_SECONDS = 2.0
_READY = "Ready"


class FluxImage(MetricMixin):
    category = "image"
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "Black Forest Labs FLUX (submit + poll REST API)",
        "requires_api_key": True,
        "supports_model_listing": False,
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

    def _headers(self) -> dict[str, str]:
        return {"x-key": self._api_key} if self._api_key else {}

    @staticmethod
    def _round32(value: int) -> int:
        return max(256, (int(value) // 32) * 32)

    async def generate_image(self, request: ImageRequest) -> ImageResult:
        width = self._round32(request.width)
        height = self._round32(request.height)
        body: dict[str, Any] = {
            "prompt": request.prompt,
            "width": width,
            "height": height,
            "output_format": request.output_format,
        }
        if request.negative_prompt:
            # ponytail: BFL FLUX.2 accepts a negative_prompt field on newer
            # models; older ones ignore unknown fields — harmless.
            body["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            body["seed"] = int(request.seed)

        started = time.monotonic()
        retry_counter: list[int] = []

        async def _submit() -> httpx.Response:
            return await self._client.post(f"/v1/{self._model}", json=body, headers=self._headers())

        async def _run() -> ImageResult:
            response = await with_retry(
                _submit,
                attempts=self._attempts,
                what="bfl.submit",
                logger_name="bfl",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="bfl.submit")
            data = response.json()
            task_id = str((data or {}).get("id") or "")
            if not task_id:
                raise PermanentError("bfl.submit: response missing task id")
            sample_url = await self._poll(task_id)
            image = await self._download(sample_url)
            return ImageResult(
                data=image,
                mime_type=f"image/{request.output_format}",
                width=width,
                height=height,
                provider=PROVIDER_NAME,
                model=self._model,
                usage={"billed_megapixels": max(1, (width * height + 999_999) // 1_000_000)},
                latency_ms=int((time.monotonic() - started) * 1000),
                retries=len(retry_counter),
            )

        return await self._observed("image.generate", len(request.prompt), _run, retry_counter)

    async def _poll(self, task_id: str) -> str:
        """Poll get_result until Ready; deadline = adapter timeout."""
        deadline = time.monotonic() + self.timeout
        params = {"id": task_id}
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            async def _call() -> httpx.Response:
                return await self._client.get(
                    "/v1/get_result", params=params, headers=self._headers()
                )

            response = await with_retry(_call, attempts=2, what="bfl.poll", logger_name="bfl")
            if response.status_code >= 500:
                raise TransientError(f"bfl.poll: {response.status_code}")
            if response.status_code >= 400:
                raise PermanentError(f"bfl.poll: HTTP {response.status_code}")
            data = response.json()
            status = str((data or {}).get("status") or "")
            if status == _READY:
                url = str(((data or {}).get("result") or {}).get("sample") or "")
                if not url:
                    raise PermanentError("bfl.poll: Ready without sample URL")
                return url
            if status in ("Error", "Content Moderation", "Request Moderation"):
                raise PermanentError(f"bfl.poll: task {status}")
        raise TransientError(f"bfl.poll: timed out after {self.timeout}s")

    async def _download(self, url: str) -> bytes:
        """Fetch the finished image; https only (never a caller-controlled URL)."""
        if not url.startswith("https://"):
            raise PermanentError("bfl.download: refusing non-https sample URL")

        async def _call() -> httpx.Response:
            return await self._client.get(url)

        response = await with_retry(_call, attempts=2, what="bfl.download", logger_name="bfl")
        if response.status_code >= 400:
            raise TransientError(f"bfl.download: HTTP {response.status_code}")
        data = response.content
        if not data:
            raise PermanentError("bfl.download: empty image payload")
        return data

    async def ping(self) -> None:
        """BFL has no cheap health endpoint: an authenticated get_result probe
        returning any HTTP status proves reachability + auth (400 = live)."""

        async def _call() -> httpx.Response:
            return await self._client.get(
                "/v1/get_result", params={"id": "ping"}, headers=self._headers()
            )

        response = await with_retry(_call, attempts=1, what="bfl.ping", logger_name="bfl")
        if response.status_code in (401, 403):
            raise PermanentError(f"bfl.ping: auth failed (HTTP {response.status_code})")
