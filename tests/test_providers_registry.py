"""Provider registry + observability tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.providers.base import PermanentError
from app.providers.embedding.openai_compat import OpenAICompatEmbedding
from app.providers.llm.gemini import GeminiLLM
from app.providers.llm.openai_compat import OpenAICompatLLM
from app.providers.metrics import EventObserver, LoggingObserver, ProviderCallRecord
from app.providers.registry import ProviderRegistry
from app.providers.vector.qdrant_store import QdrantStore
from app.repositories.integrations import IntegrationRepo
from app.services.secrets import SecretsService
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    return pb.collection("projects").create(
        {
            "name": "P",
            "slug": "proj",
            "language": "en",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )


def add_integration(
    pb: FakePocketBase,
    project_id: str,
    category: str,
    provider: str,
    *,
    base_url: str = "",
    model: str = "",
    api_key: str = "k-123",
) -> dict[str, Any]:
    secrets = SecretsService(b"0123456789abcdef0123456789abcdef")
    return IntegrationRepo(pb).create(
        project=project_id,
        category=category,
        provider=provider,
        display_name=f"{category}-{provider}",
        model=model,
        configuration={"base_url": base_url, "model": model},
        secrets_enc=secrets.encrypt(json.dumps({"api_key": api_key})),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Dynamic resolution
# ---------------------------------------------------------------------------
def test_registry_resolves_by_provider_name():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    add_integration(pb, project["id"], "llm", "gemini")
    settings = {
        "defaultLlmProvider": "gemini",
        "defaultLlmModel": "gemini-2.0-flash",
        "retryPolicy": {"max_attempts": 2},
    }
    llm = registry.get_llm_provider(project, settings)
    assert isinstance(llm, GeminiLLM)
    assert llm.model_name == "gemini-2.0-flash"

    # switch provider name in settings → different adapter, same registry
    add_integration(pb, project["id"], "llm", "openai_compat")
    settings["defaultLlmProvider"] = "openai_compat"
    llm2 = registry.get_llm_provider(project, settings)
    assert isinstance(llm2, OpenAICompatLLM)


def test_registry_embedding_is_openai_compatible_only():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    add_integration(pb, project["id"], "embedding", "openai_compat", model="text-embedding-3-small")

    # The connection's explicitly configured model is authoritative.
    embedding = registry.get_embedding_provider(
        project, {"embeddingModel": "", "embeddingDimensions": 0, "retryPolicy": {}}
    )
    assert isinstance(embedding, OpenAICompatEmbedding)
    assert embedding.model_name == "text-embedding-3-small"
    assert embedding.dimensions == 1536

    with pytest.raises(PermanentError, match="enter the model ID"):
        registry._config_embedding(
            project,
            {"category": "embedding", "configuration": {}},
            {"embeddingModel": "", "embeddingDimensions": 0},
            "openai_compat",
        )

    # a stale stored provider name must not break resolution
    stale = registry.get_embedding_provider(
        project,
        {
            "embeddingProvider": "cohere",
            "embeddingModel": "text-embedding-3-small",
            "embeddingDimensions": 1536,
            "retryPolicy": {},
        },
    )
    assert isinstance(stale, OpenAICompatEmbedding)
    assert stale.dimensions == 1536


def test_registry_embedding_normalizes_scheme_less_base_url():
    """Scheme-less base URLs (the 'unsupported URL scheme' failure) get https://
    prepended before validation, so health checks and calls work."""
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    add_integration(pb, project["id"], "embedding", "openai_compat", base_url="api.openai.com/v1")

    embedding = registry.get_embedding_provider(
        project,
        {
            "embeddingProvider": "openai_compat",
            "embeddingModel": "text-embedding-3-small",
            "embeddingDimensions": 1536,
            "retryPolicy": {},
        },
    )
    assert isinstance(embedding, OpenAICompatEmbedding)
    assert embedding._base_url == "https://api.openai.com/v1"


def test_registry_unknown_provider_raises_permanently():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    add_integration(pb, project["id"], "llm", "openai_compat")
    with pytest.raises(PermanentError):
        registry.get_llm_provider(
            project,
            {"defaultLlmProvider": "does-not-exist", "defaultLlmModel": "m", "retryPolicy": {}},
        )


def test_registry_missing_integration_raises_permanently():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    with pytest.raises(PermanentError):
        registry.get_llm_provider(
            project,
            {"defaultLlmProvider": "openai_compat", "defaultLlmModel": "m", "retryPolicy": {}},
        )


def test_registry_reranker_returns_none_without_integration():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    assert registry.get_reranker_provider(project, {}) is None


def test_registry_available_providers():
    pb = make_pb()
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    llm_providers = registry.available_providers("llm")
    assert "openai_compat" in llm_providers
    assert "gemini" in llm_providers
    assert "cohere" not in registry.available_providers("embedding")
    assert registry.available_providers("embedding") == ["openai_compat"]
    assert "qdrant" in registry.available_providers("vector_store")
    assert "wordpress" in registry.available_providers("publisher")


def test_registry_vector_uses_env_fallback_without_integration():
    pb = make_pb()
    project = make_project(pb)
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    vector = registry.get_vector_provider(
        project, {"embeddingModel": "embed-v4.0", "retryPolicy": {}}
    )
    assert isinstance(vector, QdrantStore)
    assert "ezdistro-proj-" in vector._namespace  # namespaced per project+model


# ---------------------------------------------------------------------------
# Health checks (test integration without revealing the secret)
# ---------------------------------------------------------------------------
def test_test_integration_updates_health():
    pb = make_pb()
    project = make_project(pb)
    integration = add_integration(
        pb, project["id"], "vector_store", "qdrant", base_url="http://127.0.0.1:6333"
    )
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    # no network in tests → ping fails → unhealthy; secret never revealed
    result = asyncio.run(registry.test_integration(project, integration))
    assert result["ok"] is False
    assert result["latency_ms"] >= 0
    assert result["models"] == []
    updated = IntegrationRepo(pb).get(integration["id"])
    assert updated["healthStatus"] == "unhealthy"
    assert "k-123" not in json.dumps(updated)  # secret never stored plaintext


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
def test_logging_observer_never_logs_content(monkeypatch):
    captured = {}

    class FakeLog:
        def debug(self, *args, **kwargs):
            captured.update(kwargs)

        def warning(self, *args, **kwargs):
            captured.update(kwargs)

    observer = LoggingObserver(log=FakeLog())  # type: ignore[arg-type]
    observer.on_call(
        ProviderCallRecord(
            provider="p",
            model="m",
            operation="op",
            latency_ms=5,
            success=True,
            request_chars=123,
            prompt_tokens=1,
            completion_tokens=2,
        )
    )
    assert captured["request_chars"] == 123
    assert "content" not in captured
    assert captured["prompt_tokens"] == 1


def test_logging_observer_accepts_keyword_fields_on_failure():
    """Failed-record logging must not crash: the observer logs structlog-style
    keyword fields, which a raw stdlib Logger._log() would reject with
    "unexpected keyword argument 'provider'" (broke 'test connection')."""
    observer = LoggingObserver()
    observer.on_call(
        ProviderCallRecord(
            provider="p",
            model="m",
            operation="op",
            latency_ms=5,
            success=False,
            error_category="transient",
            error_message="boom",
            retries=1,
        )
    )


def test_event_observer_writes_job_events_without_content():
    pb = make_pb()
    project = make_project(pb)
    job = pb.collection("jobs").create(
        {
            "project": project["id"],
            "type": "t",
            "status": "running",
            "idempotencyKey": "x",
            "maxAttempts": 3,
            "attempts": 0,
        }
    )
    from app.repositories.jobs import JobEventRepo

    events = JobEventRepo(pb)
    observer = EventObserver(events, project["id"], job["id"])
    observer.on_call(
        ProviderCallRecord(
            provider="cohere",
            model="embed-v4.0",
            operation="embedding.search_query",
            latency_ms=88,
            success=True,
            request_chars=77,
        )
    )

    rows = pb.collection("job_events").get_full_list()
    assert len(rows) == 1
    assert rows[0]["eventType"] == "provider_call"
    assert rows[0]["metadata"]["provider"] == "cohere"
    assert rows[0]["metadata"]["latency_ms"] == 88
    assert "content" not in rows[0]["metadata"]
