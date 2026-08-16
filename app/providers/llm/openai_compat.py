"""OpenAI-compatible LLM adapter (chat.completions).

Works with OpenAI, DeepSeek, Azure-compatible gateways, Ollama (`/v1`), and any
server exposing `POST {base}/chat/completions`.

Implements the full LLMProvider protocol: generate, generate_json, streaming,
usage + latency metrics, model metadata, per-call timeout.
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
from app.providers.http import (
    acquire_async_client,
    raise_for_provider,
    validate_llm_json_response,
    with_retry,
)
from app.providers.metrics import MetricMixin

PROVIDER_NAME = "openai_compat"


class OpenAICompatLLM(MetricMixin):
    category = "llm"
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "OpenAI-compatible chat.completions (OpenAI, DeepSeek, Ollama /v1, any compatible endpoint)",
        "supports_temperature": True,
        "supports_model_listing": True,
        "requires_api_key": False,
        "default_base_url": "https://api.openai.com/v1",
    }

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 120.0,
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
    def _build_body(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams,
        json_mode: bool,
        stream: bool = False,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
        return body

    def _request(self, body: dict[str, Any], retry_counter: list[int]) -> Any:
        async def _call() -> httpx.Response:
            return await self._client.post("/chat/completions", json=body)

        return with_retry(
            _call,
            attempts=self._attempts,
            what="llm.generate",
            logger_name="llm",
            retry_counter=retry_counter,
        )

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
            response = await self._request(body, retry_counter)
            raise_for_provider(response, what="llm.generate")
            data = validate_llm_json_response(response.json(), "llm.generate")
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return LLMResult(
                text=content,
                provider=PROVIDER_NAME,
                model=self._model,
                usage={
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                },
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
            response = await self._request(body, retry_counter)
            raise_for_provider(response, what="llm.generate_json")
            data = validate_llm_json_response(response.json(), "llm.generate_json")
            content = data["choices"][0]["message"]["content"]
            # Robust extraction: fence-stripping + balanced-brace scan.
            from app.domain.parsing import extract_json

            parsed = extract_json(content)
            if not isinstance(parsed, dict):
                raise PermanentError("llm.generate_json: model did not return a JSON object")
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
        body = self._build_body(
            system=system, user=user, params=params, json_mode=False, stream=True
        )
        retry_counter: list[int] = []

        async def _run() -> AsyncIterator[str]:
            response = await self._request(body, retry_counter)
            raise_for_provider(response, what="llm.stream")
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                    continue
                if delta:
                    yield delta

        generator = self._observed_stream(_run, "llm.stream", len(user), retry_counter)
        async for chunk in generator:
            yield chunk

    async def ping(self) -> None:
        """Health probe — a 1-token completion. Raises ProviderError on failure."""
        retry_counter: list[int] = []

        async def _run() -> None:
            response = await self._request(
                self._build_body(
                    system=None,
                    user="ping",
                    params=GenerationParams(temperature=0, max_tokens=1),
                    json_mode=False,
                ),
                retry_counter,
            )
            raise_for_provider(response, what="llm.ping")
            validate_llm_json_response(response.json(), "llm.ping")

        await self._observed("llm.ping", 4, _run, retry_counter)

    async def list_models(self) -> list[str]:
        """GET /models — returns available model ids ([] when unsupported)."""
        retry_counter: list[int] = []

        async def _run() -> list[str]:
            async def _call() -> httpx.Response:
                return await self._client.get("/models")

            response = await with_retry(
                _call,
                attempts=2,
                what="llm.list_models",
                logger_name="llm",
                retry_counter=retry_counter,
            )
            raise_for_provider(response, what="llm.list_models")
            data = response.json()
            models = data.get("data") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return []
            return sorted(str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id"))

        return await self._observed("llm.list_models", 0, _run, retry_counter)
