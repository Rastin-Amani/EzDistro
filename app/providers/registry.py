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
CATEGORY_IMAGE = "image"
CATEGORY_SERP = "serp"
CATEGORY_GOOGLE_ADS = "google_ads"

# Embeddings are OpenAI-compatible only (OpenAI, Azure, 9Router, Ollama, vLLM…).
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536


def _integration_model(integration: dict[str, Any]) -> str:
    """Read the dedicated DB field, retaining compatibility with old JSON rows."""
    config = integration.get("configuration") or {}
    return str(integration.get("model") or config.get("model") or "").strip()


CATEGORIES = (
    CATEGORY_LLM,
    CATEGORY_EMBEDDING,
    CATEGORY_RERANKER,
    CATEGORY_VECTOR,
    CATEGORY_PUBLISHER,
    CATEGORY_IMAGE,
    CATEGORY_SERP,
    CATEGORY_GOOGLE_ADS,
)

DEFAULT_PROVIDER: dict[str, str] = {
    CATEGORY_LLM: "openai_compat",
    CATEGORY_EMBEDDING: "openai_compat",
    CATEGORY_RERANKER: "cohere_compat",
    CATEGORY_VECTOR: "qdrant",
    CATEGORY_PUBLISHER: "wordpress",
    CATEGORY_IMAGE: "gemini",
    CATEGORY_SERP: "none",
    CATEGORY_GOOGLE_ADS: "google_ads",
}

SETTINGS_KEY: dict[str, str] = {
    CATEGORY_LLM: "defaultLlmProvider",
    CATEGORY_EMBEDDING: "embeddingProvider",
    CATEGORY_RERANKER: "rerankerProvider",
    CATEGORY_IMAGE: "imageProvider",
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
        from app.providers.embedding.openai_compat import OpenAICompatEmbedding
        from app.providers.google_ads_oauth import GoogleAdsOAuthProvider
        from app.providers.image.flux import FluxImage
        from app.providers.image.gemini import GeminiImage
        from app.providers.image.openai_compat import OpenAICompatImage
        from app.providers.llm.gemini import GeminiLLM
        from app.providers.llm.openai_compat import OpenAICompatLLM
        from app.providers.publish.wordpress import WordPressPublisher
        from app.providers.rerank.cohere_compat import CohereCompatReranker
        from app.providers.serp import NoneSERPProvider, SerperSERPProvider
        from app.providers.vector.qdrant_store import QdrantStore

        for cls in (
            OpenAICompatLLM,
            GeminiLLM,
            OpenAICompatEmbedding,
            CohereCompatReranker,
            QdrantStore,
            WordPressPublisher,
            GeminiImage,
            FluxImage,
            OpenAICompatImage,
            NoneSERPProvider,
            SerperSERPProvider,
            GoogleAdsOAuthProvider,
        ):
            adapter: Any = cls
            self._table[(adapter.category, adapter.provider_name)] = cls
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

    def get_image_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
        role_config: dict[str, Any] | None = None,
    ) -> Any:
        """Image-generation provider; role_config carries {provider, model}."""
        return self._resolve(
            CATEGORY_IMAGE,
            project,
            settings,
            observer,
            integration,
            role_config=role_config,
        )

    def get_serp_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> Any:
        """Optional SERP provider. No serp integration → the `none` provider,
        which reports 'uncollected' instead of fabricating ranking data."""
        from app.providers.serp import NoneSERPProvider

        integration = integration or self._integrations.get_active(project["id"], CATEGORY_SERP)
        if not integration:
            return NoneSERPProvider()
        provider_name = str(integration.get("provider") or "")
        if not provider_name or provider_name == "none":
            return NoneSERPProvider()
        return self._resolve(CATEGORY_SERP, project, settings, observer, integration)

    def get_google_ads_provider(
        self,
        project: dict[str, Any],
        settings: dict[str, Any],
        observer: CallObserver | None = None,
        integration: dict[str, Any] | None = None,
    ) -> Any:
        """Google Ads OAuth client for Keyword Planner research.

        Returns None when the project has no enabled Google Ads connection —
        callers treat that as "research is not configured for this project".
        """
        integration = integration or self._integrations.get_active(
            project["id"], CATEGORY_GOOGLE_ADS
        )
        if not integration:
            return None
        return self._resolve(CATEGORY_GOOGLE_ADS, project, settings, observer, integration)

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
        elif category == CATEGORY_IMAGE:
            provider_name = str(
                (role_config or {}).get("provider")
                or settings.get("imageProvider")
                or DEFAULT_PROVIDER[category]
            )
        elif category == CATEGORY_SERP:
            provider_name = str((integration or {}).get("provider") or DEFAULT_PROVIDER[category])
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
        # Embeddings are OpenAI-compatible only — there is exactly one adapter,
        # so a stale stored value (e.g. old "cohere") must not fail resolution.
        if category == CATEGORY_EMBEDDING:
            return DEFAULT_PROVIDER[category]
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
            CATEGORY_IMAGE: self._config_image,
            CATEGORY_SERP: self._config_serp,
            CATEGORY_GOOGLE_ADS: self._config_google_ads,
        }
        builder = builders.get(category)
        if builder is None:
            raise PermanentError(f"no config builder for category {category!r}")
        if category in (CATEGORY_LLM, CATEGORY_IMAGE):
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
        # Model source order: the connection's own model (set on the Connections
        # tab — authoritative) → explicit per-role/global override → project default.
        # A hardcoded fallback is deliberately avoided: a fabricated model ID
        # makes providers fail with opaque "no credentials" errors.
        model = (
            _integration_model(integration)
            or rc.get("model")
            or settings.get("defaultLlmModel")
            or ""
        )
        if not model:
            raise PermanentError(
                f"no {role} model configured — set the model on the Connections tab (or in AI Models)"
            )
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
        base = "https://api.openai.com/v1"
        model = _integration_model(integration) or str(settings.get("embeddingModel") or "").strip()
        if not model:
            raise PermanentError(
                "no embedding model configured — enter the model ID on the Connections tab"
            )
        return {
            "base_url": self._safe_url(integration, base),
            "model": model,
            "dimensions": int(settings.get("embeddingDimensions") or DEFAULT_EMBEDDING_DIMENSIONS),
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
        model = _integration_model(integration) or str(settings.get("rerankerModel") or "").strip()
        if not model:
            raise PermanentError(
                "no reranker model configured — enter the model ID on the Connections tab"
            )
        return {
            "base_url": self._safe_url(integration, "https://api.cohere.com/v1"),
            "model": model,
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
        model = settings.get("embeddingModel") or DEFAULT_EMBEDDING_MODEL
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

    # -- image generation ------------------------------------------------------------
    def _config_image(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
        role: str = "cover",
        role_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_IMAGE)
        # Image adapters speak incompatible protocols (Gemini-native vs OpenAI
        # gateway vs BFL). Falling back to a *different* provider's integration
        # silently produces garbage (e.g. BFL paths on an OpenAI gateway → HTML
        # 404), so fail fast with an actionable message instead.
        integration_provider = str(integration.get("provider") or "")
        if provider_name and integration_provider and integration_provider != provider_name:
            raise PermanentError(
                f"image provider '{provider_name}' is configured for this role, but the "
                f"active image integration is '{integration_provider}' — set the role's "
                f"provider to '{integration_provider}' in project image settings (or add a "
                f"'{provider_name}' integration)"
            )
        base = {
            "bfl": "https://api.bfl.ai",
            "openai_compat": "https://api.openai.com/v1",
        }.get(provider_name, "https://generativelanguage.googleapis.com/v1beta")
        model = (role_config or {}).get("model") or _integration_model(integration) or ""
        if not model:
            raise PermanentError(
                f"no image model configured for {role} — set it in project settings"
            )
        return {
            "base_url": self._safe_url(integration, base),
            "model": str(model),
            "api_key": self._api_key(integration),
            "attempts": self._retries(settings),
            "timeout": 180.0,
        }

    # -- serp (optional) ------------------------------------------------------------
    def _config_serp(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_SERP)
        base = {
            "serper": "https://google.serper.dev",
        }.get(provider_name, "")
        if not base:
            raise PermanentError(f"unknown serp provider: {provider_name!r}")
        return {
            "base_url": self._safe_url(integration, base),
            "api_key": self._api_key(integration),
            "attempts": self._retries(settings),
            "timeout": 45.0,
        }

    # -- google ads oauth client (optional) -----------------------------------------
    def _config_google_ads(
        self,
        project: dict[str, Any],
        integration: dict[str, Any] | None,
        settings: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        integration = self._integration_or_error(integration, CATEGORY_GOOGLE_ADS)
        config = integration.get("configuration") or {}
        secrets = self._decrypt_secrets(integration)
        return {
            "client_id": str(config.get("client_id") or ""),
            "client_secret": str(secrets.get("client_secret") or ""),
            "redirect_uri": str(config.get("redirect_uri") or ""),
            "api_version": str(config.get("api_version") or "v25"),
            "login_customer_id": str(config.get("login_customer_id") or ""),
            "developer_token": str(secrets.get("developer_token") or ""),
            "timeout": 45.0,
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
        # Only categories whose providers expose a `/models`-style listing.
        getters_any: dict[str, Any] = {
            CATEGORY_LLM: self.get_llm_provider,
            CATEGORY_EMBEDDING: self.get_embedding_provider,
            CATEGORY_RERANKER: self.get_reranker_provider,
            CATEGORY_IMAGE: self.get_image_provider,
        }
        getter: Any = getters_any.get(category)
        if getter is None:
            return []
        try:
            provider_name = integration.get("provider") or DEFAULT_PROVIDER.get(category, "")
            configured_model = _integration_model(integration) or "probe"
            # LLM resolves the model from a role; other categories read their own
            # settings key (e.g. embeddingModel); "probe" satisfies the builders.
            probe_settings: dict[str, Any] = {
                "defaultLlmProvider": provider_name,
                "defaultLlmModel": configured_model,
                "embeddingProvider": provider_name,
                "embeddingModel": configured_model,
                "rerankerProvider": provider_name,
                "rerankModel": configured_model,
                "imageProvider": provider_name,
            }
            provider = getter(
                project,
                probe_settings,
                None,
                integration=integration,
                role="outline",
                role_config={"provider": provider_name, "model": configured_model},
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
            CATEGORY_IMAGE: self.get_image_provider,
            CATEGORY_SERP: self.get_serp_provider,
            CATEGORY_GOOGLE_ADS: self.get_google_ads_provider,
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
            settings["defaultLlmModel"] = _integration_model(integration) or "probe"
        elif category == CATEGORY_EMBEDDING:
            settings["embeddingProvider"] = (
                integration.get("provider") or DEFAULT_PROVIDER[category]
            )
            settings["embeddingModel"] = _integration_model(integration) or "probe"
        elif category == CATEGORY_RERANKER:
            settings["rerankerProvider"] = integration.get("provider") or DEFAULT_PROVIDER[category]
        elif category == CATEGORY_IMAGE:
            settings["imageProvider"] = integration.get("provider") or DEFAULT_PROVIDER[category]

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

    model = settings.get("embeddingModel") or DEFAULT_EMBEDDING_MODEL
    return project_key(str(project.get("slug", project["id"])), str(model))
