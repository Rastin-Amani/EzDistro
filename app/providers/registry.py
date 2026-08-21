"""Provider registry — dynamic resolution of provider_name → adapter.

The registry is the ONLY place that knows which adapters exist. Domain code
calls `get_llm_provider(project, settings, role=...)` and receives a protocol
instance; swapping providers = registering a new adapter class, nothing else
changes. No if/else chains per provider: resolution and config extraction are
table-driven.

Also provides:
- per-provider capabilities (PROVIDER_META) for the UI (dynamic fields)
- model discovery (list_models) with a TTL cache
- "test connection" with latency + model availability
- integration resolution matching a provider name (per-role connections)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from app.config import settings as app_settings
from app.domain.validation import UnsafeUrlError, normalize_base_url, validate_url
from app.providers.base import (
    EmbeddingProvider,
    LLMProvider,
    PermanentError,
    PublisherProvider,
    RerankerProvider,
    VectorStoreProvider,
)
from app.providers.metrics import CallObserver, LoggingObserver
from app.repositories.integrations import IntegrationRepo
from app.services.secrets import SecretsService

logger = logging.getLogger(__name__)

CATEGORY_LLM = "llm"
CATEGORY_EMBEDDING = "embedding"
CATEGORY_RERANKER = "reranker"
CATEGORY_VECTOR = "vector_store"
CATEGORY_PUBLISHER = "publisher"
CATEGORIES = (
    CATEGORY_LLM,
    CATEGORY_EMBEDDING,
    CATEGORY_RERANKER,
    CATEGORY_VECTOR,
    CATEGORY_PUBLISHER,
)

DEFAULT_PROVIDER: dict[str, str] = {
    CATEGORY_LLM: "openai_compat",
    CATEGORY_EMBEDDING: "cohere",
    CATEGORY_RERANKER: "cohere_compat",
    CATEGORY_VECTOR: "qdrant",
    CATEGORY_PUBLISHER: "wordpress",
}

SETTINGS_KEY: dict[str, str] = {
    CATEGORY_LLM: "defaultLlmProvider",
    CATEGORY_EMBEDDING: "embeddingProvider",
    CATEGORY_RERANKER: "rerankerProvider",
}

# model listing cache: integration_id → (fetched_at, models)
_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODELS_TTL = 600.0


class ProviderRegistry:
    def __init__(self, pb: Any, secrets: SecretsService | None = None) -> None:
        self._pb = pb
        self._secrets_service = secrets or SecretsService()
        self._integrations = IntegrationRepo(pb)
        self._table: dict[tuple[str, str], type] = {}
        self._load_adapters()

    # ---------------------------------------------------------------------------
    # Adapter table (provider_name → class). Add a new provider here.
    # ---------------------------------------------------------------------------
    def _load_adapters(self) -> None:
        from app.providers.embedding.cohere import CohereEmbedding
        from app.providers.embedding.openai_compat import OpenAICompatEmbedding
        from app.providers.llm.gemini import GeminiLLM
        from app.providers.llm.openai_compat import OpenAICompatLLM
        from app.providers.publish.wordpress import WordPressPublisher
        from app.providers.rerank.cohere_compat import CohereCompatReranker
        from app.providers.vector.qdrant_store import QdrantStore

        for cls in (
            OpenAICompatLLM,
            GeminiLLM,
            OpenAICompatEmbedding,
            CohereEmbedding,
            CohereCompatReranker,
            QdrantStore,
            WordPressPublisher,
        ):
            self._table[(cls.category, cls.provider_name)] = cls
        # provider aliases with the same adapter but different UI metadata
        self._table[(CATEGORY_LLM, "ollama")] = OpenAICompatLLM
        self._table[(CATEGORY_LLM, "custom")] = OpenAICompatLLM

    # ---------------------------------------------------------------------------
    # Public resolution API
    # ---------------------------------------------------------------------------
    def available_providers(self, category: str) -> list[str]:
        providers = sorted(name for (cat, name) in self._table if cat == category)
        if category == CATEGORY_LLM:
            providers = [p for p in providers if p != "ollama" or app_settings.ollama_enabled]
        return providers

    def provider_metadata(self, category: str) -> list[dict[str, Any]]:
        """Capabilities per provider for the UI (dynamic field visibility)."""
        out = []
        for (cat, name), cls in self._table.items():
            if cat != category:
                continue
            if name == "ollama" and not app_settings.ollama_enabled:
                continue
            meta = dict(getattr(cls, "PROVIDER_META", None) or {})
            out.append({"provider": name, **meta})
        return sorted(out, key=lambda p: p["provider"])

    def get_llm_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
        role: str = "outline",
        role_config: dict[str, Any] | None = None,
    ) -> LLMProvider:
        return self._resolve(
            CATEGORY_LLM,
            project,
            settings,
            observer,
            integration,
            role=role,
            role_config=role_config,
        )

    def get_embedding_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> EmbeddingProvider:
        return self._resolve(CATEGORY_EMBEDDING, project, settings, observer, integration)

    def get_reranker_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> RerankerProvider | None:
        integration = integration or self._integrations.get_active(project["id"], CATEGORY_RERANKER)
        if not integration:
            return None
        return self._resolve(CATEGORY_RERANKER, project, settings, observer, integration)

    def get_vector_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> VectorStoreProvider:
        return self._resolve(CATEGORY_VECTOR, project, settings, observer, integration)

    def get_publisher_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> PublisherProvider:
        return self._resolve(CATEGORY_PUBLISHER, project, settings, observer, integration)

    def active_integration_for_provider(
        self, project_id: str, category: str, provider_name: str
    ) -> dict[str, Any] | None:
        """Active integration whose provider matches; else the active one of the category."""
        if provider_name:
            matched = self._integrations.first(
                filter=f'project="{project_id}" && category="{category}" && provider="{provider_name}" && enabled=true'
            )
            if matched:
                return matched
        return self._integrations.get_active(project_id, category)

    # ---------------------------------------------------------------------------
    # Generic resolution — table-driven, no per-provider branches
    # ---------------------------------------------------------------------------
    def _resolve(
        self,
        category: str,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None,
        integration: dict[str, Any] | None = None,
        role: str = "outline",
        role_config: dict[str, Any] | None = None,
    ) -> Any:
        if category == CATEGORY_LLM:
            provider_name = str(
                (role_config or {}).get("provider")
                or settings.get("defaultLlmProvider")
                or DEFAULT_PROVIDER[category]
            )
        else:
            provider_name = self._provider_name_for(category, settings)
        cls = self._table.get((category, provider_name))
        if cls is None:
            raise PermanentError(f"unknown {category} provider: {provider_name!r}")

        if integration is None:
            integration = self.active_integration_for_provider(
                project["id"], category, provider_name
            )
        config = self._config_for(
            category,
            project,
            integration,
            settings,
            provider_name,
            role=role,
            role_config=role_config,
        )

        adapter = cls(**config)
        if observer is not None:
            setter = getattr(adapter, "_set_observer", None)
            if setter is not None:
                setter(observer)
        return adapter

    def _provider_name_for(self, category: str, settings: dict[str, Any]) -> str:
        key = SETTINGS_KEY.get(category)
        configured = str(settings.get(key) or "") if key else ""
        return configured.strip() or DEFAULT_PROVIDER.get(category) or ""

    # ---------------------------------------------------------------------------
    # Per-category config extraction (integration + settings → adapter kwargs)
    # ---------------------------------------------------------------------------
    def _config_for(
        self,
        category: str,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
        role: str = "outline",
        role_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        builders: dict[str, Callable[..., dict[str, Any]]] = {
            CATEGORY_LLM: self._config_llm,
            CATEGORY_EMBEDDING: self._config_embedding,
            CATEGORY_RERANKER: self._config_reranker,
            CATEGORY_VECTOR: self._config_vector,
            CATEGORY_PUBLISHER: self._config_publisher,
        }
        builder = builders.get(category)
        if builder is None:
            raise PermanentError(f"no config builder for category {category!r}")
        if category == CATEGORY_LLM:
            return builder(
                project, integration, settings, provider_name, role=role, role_config=role_config
            )
        return builder(project, integration, settings, provider_name)

    def _integration_or_error(
        self, integration: dict[str, Any] | None, category: str
    ) -> dict[str, Any]:
        if integration is None:
            raise PermanentError(
                f"no active {category} integration for this project — configure it in Integrations"
            )
        return integration

    def _api_key(self, integration: dict[str, Any]) -> str:
        return str(self._decrypt_secrets(integration).get("api_key") or "")

    def _decrypt_secrets(self, integration: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            raw = self._secrets_service.decrypt(integration.get("secretsEnc") or "")
        except ValueError as exc:
            raise PermanentError(
                f"integration {integration.get('displayName', '')} cannot be decrypted: {exc}"
            ) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise PermanentError(
                f"integration {integration.get('displayName', '')} has malformed secrets"
            ) from exc

    def _safe_url(self, integration: dict[str, Any], default: str) -> str:
        raw = normalize_base_url(
            str((integration.get("configuration") or {}).get("base_url") or "")
        )
        try:
            return validate_url(raw) if raw else default
        except UnsafeUrlError as exc:
            raise PermanentError(f"unsafe {integration.get('category')} base URL: {exc}") from exc

    def _retries(self, settings: dict[str, Any], role_retry: dict[str, Any] | None = None) -> int:
        policy = role_retry or settings.get("retryPolicy") or {}
        return max(1, int(policy.get("max_attempts", 3)))

    # -- llm ------------------------------------------------------------------------
    def _config_llm(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
        role: str = "outline",
        role_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_LLM)
        rc = role_config or {}
        base = (
            "https://api.openai.com/v1"
            if provider_name in ("openai_compat", "ollama", "custom")
            else "https://generativelanguage.googleapis.com/v1beta"
        )
        model = (
            rc.get("model")
            or settings.get("defaultLlmModel")
            or (integration.get("configuration") or {}).get("model")
            or ""
        )
        if not model:
            raise PermanentError(f"no {role} model configured — set it in AI Models")
        return {
            "base_url": self._safe_url(integration, base),
            "model": str(model),
            "api_key": self._api_key(integration),
            "attempts": self._retries(settings, rc.get("retry")),
            "timeout": float(rc.get("timeout") or 120.0),
        }

    # -- embedding ------------------------------------------------------------------
    def _config_embedding(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_EMBEDDING)
        base = (
            "https://api.cohere.com/v1"
            if provider_name == "cohere"
            else "https://api.openai.com/v1"
        )
        return {
            "base_url": self._safe_url(integration, base),
            "model": str(
                settings.get("embeddingModel")
                or (integration.get("configuration") or {}).get("model")
                or "embed-v4.0"
            ),
            "dimensions": int(settings.get("embeddingDimensions") or 1024),
            "api_key": self._api_key(integration),
            "attempts": self._retries(settings),
            "timeout": 120.0,
        }

    # -- reranker -------------------------------------------------------------------
    def _config_reranker(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_RERANKER)
        return {
            "base_url": self._safe_url(integration, "https://api.cohere.com/v1"),
            "model": str(
                settings.get("rerankerModel")
                or (integration.get("configuration") or {}).get("model")
                or "rerank-v4.0"
            ),
            "api_key": self._api_key(integration),
            "attempts": self._retries(settings),
            "timeout": 60.0,
        }

    # -- vector store ---------------------------------------------------------------
    def _config_vector(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        if integration:
            url = self._safe_url(integration, app_settings.qdrant_url)
            api_key = self._api_key(integration)
        else:
            url = app_settings.qdrant_url
            api_key = app_settings.qdrant_api_key
        model = settings.get("embeddingModel") or "embed-v4.0"
        from app.providers.vector.qdrant_store import project_key

        return {
            "url": url,
            "api_key": api_key,
            "namespace": project_key(str(project.get("slug", project["id"])), str(model)),
            "timeout": 30.0,
        }

    # -- publisher ------------------------------------------------------------------
    def _config_publisher(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_PUBLISHER)
        config = integration.get("configuration") or {}
        username = str(config.get("username") or "")
        password = self._api_key(integration)
        url = self._safe_url(integration, "")
        if not username or not password:
            raise PermanentError(
                "publisher integration is missing username or application password"
            )
        if not url:
            raise PermanentError("publisher integration is missing the site base URL")
        return {
            "base_url": url,
            "username": username,
            "password": password,
            "attempts": self._retries(settings),
            "timeout": 60.0,
        }

    # ---------------------------------------------------------------------------
    # Model discovery (cached)
    # ---------------------------------------------------------------------------
    async def list_models(self, project: dict[str, Any], integration: dict[str, Any]) -> list[str]:
        """Fetch available models from the provider API (TTL-cached, []=unsupported)."""
        integration_id = integration["id"]
        now = time.monotonic()
        cached = _MODELS_CACHE.get(integration_id)
        if cached and now - cached[0] < _MODELS_TTL:
            return cached[1]

        category = integration.get("category") or ""
        getter: Callable[..., Any] | None = {
            CATEGORY_LLM: self.get_llm_provider,
        }.get(category)
        if getter is None:
            return []
        try:
            provider_name = integration.get("provider") or DEFAULT_PROVIDER.get(category, "")
            probe_settings = {
                "defaultLlmProvider": provider_name,
                "defaultLlmModel": (integration.get("configuration") or {}).get("model") or "probe",
            }
            provider = getter(
                project,
                probe_settings,
                None,
                integration=integration,
                role="outline",
                role_config={"provider": provider_name, "model": probe_settings["defaultLlmModel"]},
            )
            try:
                models = await provider.list_models()
            finally:
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()
        except Exception:
            return []
        _MODELS_CACHE[integration_id] = (now, models)
        return models

    # ---------------------------------------------------------------------------
    # Health checks
    # ---------------------------------------------------------------------------
    async def test_integration(
        self, project: dict[str, Any], integration: dict[str, Any]
    ) -> dict[str, Any]:
        """Ping the provider; returns {ok, latency_ms, message, models}.

        The integration's own provider/configuration is used (secret never
        exposed); health status + lastTestedAt are persisted.
        """
        from app.repositories.jobs import now_utc

        category = integration.get("category") or ""
        getters: dict[str, Callable[..., Any]] = {
            CATEGORY_LLM: self.get_llm_provider,
            CATEGORY_EMBEDDING: self.get_embedding_provider,
            CATEGORY_RERANKER: self.get_reranker_provider,
            CATEGORY_VECTOR: self.get_vector_provider,
            CATEGORY_PUBLISHER: self.get_publisher_provider,
        }
        getter = getters.get(category)  # type: ignore[assignment]
        if getter is None:
            self._integrations.mark_health(integration["id"], "unknown", now_utc())
            return {"ok": False, "latency_ms": 0, "message": "unsupported category", "models": []}

        settings: dict[str, Any] = {"retryPolicy": {"max_attempts": 1}}
        if category == CATEGORY_LLM:
            settings["defaultLlmProvider"] = (
                integration.get("provider") or DEFAULT_PROVIDER[category]
            )
            settings["defaultLlmModel"] = (integration.get("configuration") or {}).get(
                "model"
            ) or "probe"
        elif category == CATEGORY_EMBEDDING:
            settings["embeddingProvider"] = (
                integration.get("provider") or DEFAULT_PROVIDER[category]
            )
            settings["embeddingModel"] = (integration.get("configuration") or {}).get(
                "model"
            ) or "embed-v4.0"
        elif category == CATEGORY_RERANKER:
            settings["rerankerProvider"] = integration.get("provider") or DEFAULT_PROVIDER[category]

        started = time.monotonic()
        models: list[str] = []
        try:
            provider = getter(project, settings, LoggingObserver(), integration=integration)
            if provider is None:
                self._integrations.mark_health(integration["id"], "degraded", now_utc())
                return {
                    "ok": False,
                    "latency_ms": 0,
                    "message": "provider unavailable",
                    "models": [],
                }
            try:
                await provider.ping()
                latency_ms = int((time.monotonic() - started) * 1000)
                # model availability where supported
                if category == CATEGORY_LLM:
                    listing = getattr(provider, "list_models", None)
                    if listing is not None:
                        try:
                            models = await listing()
                        except Exception:
                            models = []
            finally:
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()
            self._integrations.mark_health(integration["id"], "healthy", now_utc())
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "message": "connection ok",
                "models": models,
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._integrations.mark_health(integration["id"], "unhealthy", now_utc())
            return {"ok": False, "latency_ms": latency_ms, "message": str(exc)[:300], "models": []}


def qdrant_namespace(project: dict[str, Any], settings: dict[str, Any]) -> str:
    from app.providers.vector.qdrant_store import project_key

    model = settings.get("embeddingModel") or "embed-v4.0"
    return project_key(str(project.get("slug", project["id"])), str(model))
