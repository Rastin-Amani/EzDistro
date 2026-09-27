"""LLM configuration tests: role resolution (project → global → legacy),
model discovery with cache, test-connection details, masked secrets, audit
events on model change, and role-aware writer usage."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.jobs.context import ProviderStack
from app.providers.base import PermanentError
from app.providers.llm.gemini import GeminiLLM
from app.providers.llm.openai_compat import OpenAICompatLLM
from app.providers.registry import ProviderRegistry
from app.repositories.app_settings import AppSettingsRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.projects import DEFAULT_SETTINGS, ProjectRepo, ProjectSettingsRepo
from app.services.secrets import SecretsService
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def make_project(pb: FakePocketBase, **overrides: Any) -> dict[str, Any]:
    project = ProjectRepo(pb).create(name="\u067e", slug="proj", language="fa")
    settings = {**DEFAULT_SETTINGS, **overrides}
    pb.collection("project_settings").create({"project": project["id"], **settings})
    return project


def add_integration(
    pb: FakePocketBase,
    project_id: str,
    provider: str,
    *,
    base_url: str = "https://example.com/v1",
    api_key: str = "sk-secret-1234",
) -> dict[str, Any]:
    secrets = SecretsService(b"0123456789abcdef0123456789abcdef")
    return IntegrationRepo(pb).create(
        project=project_id,
        category="llm",
        provider=provider,
        display_name=f"LLM {provider}",
        configuration={"base_url": base_url, "masked": "************1234"},
        secrets_enc=secrets.encrypt(json.dumps({"api_key": api_key})),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Role resolution: project override → global default → legacy
# ---------------------------------------------------------------------------
def test_role_llm_resolution_priority():
    pb = make_pb()
    project = make_project(pb)
    config = ProjectConfig.load(pb, project["id"])

    # no project override, no global → legacy fields
    outline = config.role_llm("outline")
    assert outline["provider"] == "openai_compat"
    assert outline["model"] == "gpt-4o-mini"
    assert outline["temperature"] == 0.7
    assert outline["timeout"] == 120.0

    # global defaults apply when project fields are empty
    AppSettingsRepo(pb).set_llm_defaults(
        {
            "outline": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "temperature": 0.3,
                "max_tokens": 2048,
                "timeout": 90,
            },
            "section": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "temperature": 0.5,
                "max_tokens": 2048,
                "timeout": 90,
            },
            "meta": {"provider": "", "model": ""},
            "review": {"provider": "", "model": ""},
        }
    )
    config2 = ProjectConfig.load(pb, project["id"])
    outline = config2.role_llm("outline")
    assert outline["provider"] == "gemini"
    assert outline["model"] == "gemini-2.0-flash"
    assert outline["temperature"] == 0.3
    assert outline["timeout"] == 90.0

    # project override wins over global
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"outlineProvider": "openai_compat", "outlineModel": "gpt-4o"}
    )
    config3 = ProjectConfig.load(pb, project["id"])
    outline = config3.role_llm("outline")
    assert outline["provider"] == "openai_compat"
    assert outline["model"] == "gpt-4o"
    # section still uses global
    assert config3.role_llm("section")["provider"] == "gemini"


def test_role_llm_unknown_role_rejected():
    pb = make_pb()
    project = make_project(pb)
    config = ProjectConfig.load(pb, project["id"])
    with pytest.raises(ValueError):
        config.role_llm("nope")


def test_registry_builds_role_provider():
    pb = make_pb()
    project = make_project(pb, outlineProvider="gemini", outlineModel="gemini-2.0-flash")
    add_integration(pb, project["id"], "gemini")
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    config = ProjectConfig.load(pb, project["id"])
    llm = registry.get_llm_provider(
        project, config.settings, role="outline", role_config=config.role_llm("outline")
    )
    assert isinstance(llm, GeminiLLM)
    assert llm.model_name == "gemini-2.0-flash"

    # section role without provider override → defaults to openai_compat (legacy)
    section = registry.get_llm_provider(
        project, config.settings, role="section", role_config=config.role_llm("section")
    )
    assert isinstance(section, OpenAICompatLLM)


def test_provider_stack_llm_for_roles():
    pb = make_pb()
    project = make_project(pb)
    registry = FakeRegistry()
    config = ProjectConfig.load(pb, project["id"])
    stack = ProviderStack(registry=registry, config=config)
    assert stack.llm is stack.llm_for("outline")  # cached
    assert stack.llm_for("section") is not None
    assert len(stack._llm) == 2
    asyncio.run(stack.aclose())


# ---------------------------------------------------------------------------
# Model discovery (mocked /models) + cache
# ---------------------------------------------------------------------------
def openai_models_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "gpt-5"}]},
    )


def gemini_models_handler(request: httpx.Request) -> httpx.Response:
    assert "key=test-key" in str(request.url)
    return httpx.Response(
        200,
        json={"models": [{"name": "models/gemini-2.0-flash"}, {"name": "models/gemini-2.5-pro"}]},
    )


def test_model_discovery_openai_compat():
    pb = make_pb()
    project = make_project(pb)
    integration = add_integration(pb, project["id"], "openai_compat")
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    # patch the adapter's client with a mock transport via a builder spy
    import app.providers.llm.openai_compat as mod

    original = mod.acquire_async_client

    def fake_build(base_url, *, api_key="", timeout=120.0, transport=None):
        return original(
            base_url,
            api_key=api_key,
            timeout=timeout,
            transport=httpx.MockTransport(openai_models_handler),
        )

    mod.acquire_async_client = fake_build  # type: ignore[assignment]
    try:
        models = asyncio.run(registry.list_models(project, integration))
        assert "gpt-4o" in models
        assert "gpt-5" in models
        # cached: second call does not re-fetch
        cached = asyncio.run(registry.list_models(project, integration))
        assert cached == models
    finally:
        mod.acquire_async_client = original  # type: ignore[assignment]


def test_model_discovery_gemini():
    pb = make_pb()
    project = make_project(pb)
    integration = add_integration(
        pb,
        project["id"],
        "gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
    )
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    import app.providers.llm.gemini as mod

    original = mod.acquire_async_client

    def fake_build(base_url, *, api_key="", timeout=120.0, transport=None):
        return original(
            base_url,
            api_key=api_key,
            timeout=timeout,
            transport=httpx.MockTransport(gemini_models_handler),
        )

    mod.acquire_async_client = fake_build  # type: ignore[assignment]
    try:
        models = asyncio.run(registry.list_models(project, integration))
        assert "gemini-2.0-flash" in models
        assert "gemini-2.5-pro" in models
    finally:
        mod.acquire_async_client = original  # type: ignore[assignment]


def test_model_discovery_unsupported_returns_empty():
    pb = make_pb()
    project = make_project(pb)
    integration = add_integration(pb, project["id"], "custom")
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    # custom provider: no model listing endpoint → [] (manual entry allowed)
    assert asyncio.run(registry.list_models(project, integration)) == []


# ---------------------------------------------------------------------------
# Test connection: latency + models + health
# ---------------------------------------------------------------------------
def test_test_connection_returns_latency_and_models():
    pb = make_pb()
    project = make_project(pb)
    integration = add_integration(pb, project["id"], "openai_compat")
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))

    import app.providers.llm.openai_compat as mod

    original = mod.acquire_async_client

    def fake_build(base_url, *, api_key="", timeout=120.0, transport=None):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return openai_models_handler(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {},
                },
            )

        return original(
            base_url, api_key=api_key, timeout=timeout, transport=httpx.MockTransport(handler)
        )

    mod.acquire_async_client = fake_build  # type: ignore[assignment]
    try:
        result = asyncio.run(registry.test_integration(project, integration))
        assert result["ok"] is True
        assert result["latency_ms"] >= 0
        assert "gpt-4o" in result["models"]  # model availability
        assert IntegrationRepo(pb).get(integration["id"])["healthStatus"] == "healthy"
    finally:
        mod.acquire_async_client = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Security: masked secret, never plaintext
# ---------------------------------------------------------------------------
def test_integration_record_never_contains_plaintext_secret():
    pb = make_pb()
    project = make_project(pb)
    add_integration(pb, project["id"], "openai_compat", api_key="sk-SUPER-SECRET-99")
    records = IntegrationRepo(pb).list_for_project(project["id"])
    dumped = json.dumps(records)
    assert "sk-SUPER-SECRET-99" not in dumped  # browser/DB never receives the real secret
    assert records[0]["configuration"]["masked"] == "************1234"  # masked preview only


# ---------------------------------------------------------------------------
# Audit on model change
# ---------------------------------------------------------------------------
def test_model_change_creates_audit_event():
    from app.repositories.jobs import JobEventRepo

    pb = make_pb()
    project = make_project(pb)
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"outlineProvider": "openai_compat", "outlineModel": "gpt-4o-mini"}
    )

    # simulate the save flow's audit helper
    from app.api.projects import _audit_model_changes

    class FakeRequest:
        def __init__(self, pb):
            self.state = type("S", (), {"pb": pb})()

    before = ProjectSettingsRepo(pb).get_for_project(project["id"])
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"outlineProvider": "gemini", "outlineModel": "gemini-2.0-flash"}
    )
    after = ProjectSettingsRepo(pb).get_for_project(project["id"])
    _audit_model_changes(FakeRequest(pb), project["id"], {"id": "user-1"}, before, after)

    events = pb.collection("job_events").get_full_list(
        {"filter": 'eventType="config.model_changed"'}
    )
    assert len(events) == 1
    meta = events[0]["metadata"]
    assert meta["role"] == "outline"
    assert meta["previous"] == {
        "provider": "openai_compat",
        "model": "gpt-4o-mini",
    }  # previous preserved
    assert meta["new"] == {"provider": "gemini", "model": "gemini-2.0-flash"}
    assert meta["changed_by"] == "user-1"

    # unchanged roles produce no events
    ProjectSettingsRepo(pb).upsert(
        project["id"], {"sectionProvider": "openai_compat", "sectionModel": "gpt-4o-mini"}
    )
    before = ProjectSettingsRepo(pb).get_for_project(project["id"])
    ProjectSettingsRepo(pb).upsert(project["id"], {"sectionModel": "gpt-4o-mini"})  # same values
    after = ProjectSettingsRepo(pb).get_for_project(project["id"])
    _audit_model_changes(FakeRequest(pb), project["id"], {"id": "user-1"}, before, after)
    assert (
        len(
            pb.collection("job_events").get_full_list(
                {"filter": 'eventType="config.model_changed"'}
            )
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Provider metadata for dynamic UI
# ---------------------------------------------------------------------------
def test_provider_metadata_capabilities():
    from app.config import settings as cfg

    pb = make_pb()
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    meta = {m["provider"]: m for m in registry.provider_metadata("llm")}
    assert meta["gemini"]["supports_model_listing"] is True
    assert meta["gemini"]["requires_api_key"] is True
    assert meta["openai_compat"]["supports_temperature"] is True
    assert "custom" in meta  # custom OpenAI-compatible endpoint

    # ollama hidden unless intentionally enabled
    assert "ollama" not in meta
    cfg.ollama_enabled = True
    try:
        meta2 = {m["provider"]: m for m in registry.provider_metadata("llm")}
        assert "ollama" in meta2
    finally:
        cfg.ollama_enabled = False


def test_image_provider_mismatch_fails_fast():
    """Role says bfl but the only active image integration is openai_compat →
    clear permanent error instead of BFL paths against an OpenAI gateway."""
    pb = make_pb()
    project = make_project(pb, imageInteriorProvider="bfl", imageInteriorModel="flux-2-klein-9b")
    IntegrationRepo(pb).create(
        project=project["id"],
        category="image",
        provider="openai_compat",
        display_name="AvalAI",
        configuration={"base_url": "https://api.avalai.ir/v1", "model": "gemini-3-pro-image"},
        secrets_enc="",
        enabled=True,
        created_by="u1",
    )
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    config = ProjectConfig.load(pb, project["id"])
    with pytest.raises(PermanentError, match="active image integration is 'openai_compat'"):
        registry.get_image_provider(
            project, config.settings, role_config={"provider": "bfl", "model": "flux-2-klein-9b"}
        )

    # matching provider → builds fine
    ok = registry.get_image_provider(
        project,
        config.settings,
        role_config={"provider": "openai_compat", "model": "gemini-3-pro-image"},
    )
    assert ok.model_name == "gemini-3-pro-image"
    assert ok._base_url == "https://api.avalai.ir/v1"
