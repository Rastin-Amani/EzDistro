"""Native Google Gemini LLM adapter (REST, no SDK dependency).

Uses `POST {base}/models/{model}:generateContent` (+ `:streamGenerateContent`)
with an API key. Implemented over httpx so it is fully mockable and adds no
new dependencies. Schema: https://ai.google.dev/api/generate-content
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers.base import (
    GenerationParams,
    LLMResult,
    ModelInfo,
    PermanentError,
)
from app.providers.http import acquire_async_client, raise_for_provider, with_retry
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "gemini"
DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiLLM(MetricMixin):
    category = "llm"
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "Native Google Gemini (generateContent / streamGenerateContent)",
        "supports_temperature": True,
        "supports_model_listing": True,
        "requires_api_key": True,
        "default_base_url": DEFAULT_BASE,
    }

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        model: str,
        api_key: str,
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
        self._client = acquire_async_client(
            base_url, api_key="", timeout=timeout, transport=transport
        )
        if observer is not None:
            self._set_observer(observer)
        self.model_info = ModelInfo(
            provider=PROVIDER_NAME,
            name=model,
            supports_streaming=True,
            supports_json=True,
        )
        self.model_name = model

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    # ------------------------------------------------------------------ core
    def _url(self, endpoint: str) -> str:
        return f"/models/{self._model}:{endpoint}"

    def _params(self) -> dict[str, str]:
        return {"key": self._api_key} if self._api_key else {}

    def _build_body(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams,
        json_mode: bool,
    ) -> dict[str, Any]:
        contents = [{"role": "user", "parts": [{"text": user}]}]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": params.temperature,
                "maxOutputTokens": params.max_tokens,
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return body

    async def _request(
        self, endpoint: str, body: dict[str, Any], retry_counter: list[int]
    ) -> httpx.Response:
        async def _call() -> httpx.Response:
            return await self._client.post(self._url(endpoint), params=self._params(), json=body)

        return await with_retry(
            _call,
            attempts=self._attempts,
            what=f"llm.{endpoint}",
            logger_name="gemini",
            retry_counter=retry_counter,
        )

    def _parse_response(self, data: Any, what: str) -> tuple[str, dict[str, int]]:
        if not isinstance(data, dict):
            raise PermanentError(f"gemini {what}: unexpected response shape")
        try:
            text = "".join(
                part.get("text", "") for part in data["candidates"][0]["content"]["parts"]
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentError(f"gemini {what}: malformed candidates") from exc
        if not text:
            raise PermanentError(f"gemini {what}: empty completion content")
        usage = data.get("usageMetadata") or {}
        return text, {
            "prompt_tokens": int(usage.get("promptTokenCount") or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
        }

    async def generate(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> LLMResult:
        params = params or GenerationParams(timeout=self.timeout)
        body = self._build_body(system=system, user=user, params=params, json_mode=False)
        retry_counter: list[int] = []

        async def _run() -> LLMResult:
            response = await self._request("generateContent", body, retry_counter)
            raise_for_provider(response, what="gemini.generate")
            text, usage = self._parse_response(response.json(), "generate")
            return LLMResult(
                text=text,
                provider=PROVIDER_NAME,
                model=self._model,
                usage=usage,
                retries=len(retry_counter),
            )

        return await self._observed("llm.generate", len(user), _run, retry_counter)

    async def generate_json(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> dict[str, Any]:
        params = params or GenerationParams(timeout=self.timeout)
        body = self._build_body(system=system, user=user, params=params, json_mode=True)
        retry_counter: list[int] = []

        async def _run() -> dict[str, Any]:
            response = await self._request("generateContent", body, retry_counter)
            raise_for_provider(response, what="gemini.generate_json")
            text, _usage = self._parse_response(response.json(), "generate_json")
            from app.domain.parsing import extract_json

            parsed = extract_json(text)
            if not isinstance(parsed, dict):
                raise PermanentError("gemini.generate_json: model did not return a JSON object")
            return parsed

        return await self._observed("llm.generate_json", len(user), _run, retry_counter)

    async def stream(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> AsyncIterator[str]:
        params = params or GenerationParams(timeout=self.timeout)
        body = self._build_body(system=system, user=user, params=params, json_mode=False)
        retry_counter: list[int] = []

        async def _run() -> AsyncIterator[str]:
            async def _call() -> httpx.Response:
                return await self._client.post(
                    self._url("streamGenerateContent"),
                    params={**self._params(), "alt": "sse"},
                    json=body,
                )

            response = await with_retry(
                _call,
                attempts=self._attempts,
                what="gemini.stream",
                logger_name="gemini",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="gemini.stream")
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    chunk = json.loads(payload)
                    parts = chunk["candidates"][0]["content"]["parts"]
                    delta = "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                    continue
                if delta:
                    yield delta

        generator = self._observed_stream(_run, "llm.stream", len(user), retry_counter)
        async for chunk in generator:
            yield chunk

    async def ping(self) -> None:
        retry_counter: list[int] = []

        async def _run() -> None:
            response = await self._request(
                "generateContent",
                self._build_body(
                    system=None,
                    user="ping",
                    params=GenerationParams(temperature=0, max_tokens=1),
                    json_mode=False,
                ),
                retry_counter,
            )
            raise_for_provider(response, what="gemini.ping")
            self._parse_response(response.json(), "ping")

        await self._observed("llm.ping", 4, _run, retry_counter)

    async def list_models(self) -> list[str]:
        """GET /models — returns available model ids ([] when unsupported)."""
        retry_counter: list[int] = []

        async def _run() -> list[str]:
            async def _call() -> httpx.Response:
                return await self._client.get("/models", params=self._params())

            response = await with_retry(
                _call,
                attempts=2,
                what="gemini.list_models",
                logger_name="gemini",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="gemini.list_models")
            data = response.json()
            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return []
            return sorted(
                str(m.get("name", "")).removeprefix("models/")
                for m in models
                if isinstance(m, dict)
            )

        return await self._observed("llm.list_models", 0, _run, retry_counter)
