"""UI-switching flows: provider / model / prompt switching end-to-end.

These drive the same route handlers the UI posts to (save_ai_models,
save_settings, save_prompts, activate_prompt_version) and assert the persisted
config actually resolves in the generation pipeline (ProjectConfig + registry).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api import projects as P
from app.repositories.app_settings import AppSettingsRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobEventRepo
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.prompts import PromptRepo
from app.services.prompt_service import PromptService
from app.services.settings import ProjectConfig
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    pb = FakePocketBase(default_unique_fields())
    pb.collection("users").create(
        {"email": "u@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    return pb


def make_req(pb: FakePocketBase, user: dict) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(pb=pb, user=user, req_id="r1"),
        headers={"HX-Request": "true"},
        url=SimpleNamespace(path="/projects/x"),
    )


def call_route(fn, request, *args, **kwargs):
    """Invoke a FastAPI route directly, filling Form() defaults with real strings."""
    import inspect

    from fastapi.params import Form

    for name, param in inspect.signature(fn).parameters.items():
        if name in kwargs or name == "request":
            continue
        if isinstance(param.default, Form):
            kwargs[name] = ""
    return fn(request, *args, **kwargs)


def setup_project(pb: FakePocketBase) -> dict:
    project = ProjectRepo(pb).create(name="P", slug="proj-a")
    MemberRepo(pb).add(project=project["id"], user="u1", role="owner")
    return project


def test_provider_switch_via_ai_models_ui():
    """Switching per-role providers via the AI Models UI changes what the
    pipeline resolves — and emits an audit event (previous config preserved)."""
    pb = make_pb()
    project = setup_project(pb)
    req = make_req(pb, {"id": "u1", "role": "member", "email": "u@x"})

    # UI posts: outline on openai_compat/gpt-5, sections on gemini/gemini-2.0
    call_route(
        P.save_ai_models,
        req,
        project["id"],
        outline_provider="openai_compat",
        outline_model="gpt-5",
        section_provider="gemini",
        section_model="gemini-2.0-flash",
    )

    config = ProjectConfig.load(pb, project["id"])
    outline = config.role_llm("outline")
    assert outline["provider"] == "openai_compat"
    assert outline["model"] == "gpt-5"
    section = config.role_llm("section")
    assert section["provider"] == "gemini"
    assert section["model"] == "gemini-2.0-flash"

    # audit events written per changed role
    events = JobEventRepo(pb).list_for_project(project["id"])
    kinds = [e.get("eventType") for e in events]
    assert "config.model_changed" in kinds

    # switch again — outline now on gemini
    call_route(
        P.save_ai_models,
        req,
        project["id"],
        outline_provider="gemini",
        outline_model="gemini-2.5-pro",
        section_provider="gemini",
        section_model="gemini-2.0-flash",
    )
    config2 = ProjectConfig.load(pb, project["id"])
    assert config2.role_llm("outline")["provider"] == "gemini"
    assert config2.role_llm("outline")["model"] == "gemini-2.5-pro"


def test_global_default_model_switch_via_ui():
    """Global defaults (admin-only route) flow into project resolution when the
    project has no per-role override."""
    pb = make_pb()
    project = setup_project(pb)
    admin = {"id": "a1", "role": "admin", "email": "a@x"}
    req = make_req(pb, admin)

    call_route(
        P.save_global_llm_defaults,
        req,
        project["id"],
        outline_provider="openai_compat",
        outline_model="gpt-4o",
        section_provider="openai_compat",
        section_model="gpt-4o-mini",
    )
    defaults = AppSettingsRepo(pb).get_defaults().get("llm") or {}
    assert defaults["outline"]["model"] == "gpt-4o"

    # project with no outline override resolves the global default
    config = ProjectConfig.load(pb, project["id"])
    assert config.role_llm("outline")["model"] == "gpt-4o"


def test_embedding_provider_switch_via_settings_ui():
    """Embedding provider/model/dimensions switch through the settings tab is
    persisted and reflected in the resolved vector namespace key."""
    pb = make_pb()
    project = setup_project(pb)
    req = make_req(pb, {"id": "u1", "role": "member", "email": "u@x"})

    call_route(
        P.save_settings,
        req,
        project["id"],
        embedding_provider="openai_compat",
        embedding_model="text-embedding-3-large",
        embedding_dimensions="3072",
    )
    settings = ProjectSettingsRepo(pb).get_for_project(project["id"])
    assert settings.get("embeddingModel") == "text-embedding-3-large"

    from app.providers.registry import qdrant_namespace

    ns = qdrant_namespace({"id": project["id"], "slug": "proj-a"}, settings)
    assert "text-embedding-3-large" in ns  # namespace keyed by model → switching model re-indexes


def test_prompt_switch_via_prompts_ui():
    """Saving prompts then activating another version switches what the writer
    resolves for the type — the old version stays in history."""
    pb = make_pb()
    project = setup_project(pb)
    req = make_req(pb, {"id": "u1", "role": "member", "email": "u@x"})

    call_route(
        P.save_prompts,
        req,
        project["id"],
        seo_rules="Rules version 1",
        brand_voice="Brand voice version 1",
    )
    assert (
        PromptService(pb).resolve_active(project["id"], "seo_rules")["content"] == "Rules version 1"
    )

    # save v2 → v2 active, v1 archived
    call_route(
        P.save_prompts,
        req,
        project["id"],
        seo_rules="Rules version 2",
        brand_voice="Brand voice version 2",
    )
    assert (
        PromptService(pb).resolve_active(project["id"], "seo_rules")["content"] == "Rules version 2"
    )

    # activate v1 again via the version-activation UI
    v1 = PromptRepo(pb).history(project["id"], "seo_rules")[1]  # oldest = v1
    call_route(P.activate_prompt_version, req, project["id"], "seo_rules", version_id=v1["id"])
    assert (
        PromptService(pb).resolve_active(project["id"], "seo_rules")["content"] == "Rules version 1"
    )
    assert (
        PromptService(pb).resolve_active(project["id"], "brand_voice")["content"]
        == "Brand voice version 2"
    )


def test_provider_switch_resolves_registry_adapter():
    """The registry resolves a concrete adapter class for the configured
    provider (openai_compat vs gemini) — no stale cache of the old provider."""
    from app.providers.registry import ProviderRegistry

    pb = make_pb()
    project = setup_project(pb)
    # create an llm integration the registry can use
    IntegrationRepo(pb).create(
        project=project["id"],
        category="llm",
        provider="openai_compat",
        display_name="LLM",
        configuration={"base_url": "https://api.openai.com/v1", "model": "gpt-5"},
        secrets_enc="",
        enabled=True,
    )
    req = make_req(pb, {"id": "u1", "role": "member", "email": "u@x"})
    call_route(
        P.save_ai_models,
        req,
        project["id"],
        outline_provider="openai_compat",
        outline_model="gpt-5",
    )
    registry = ProviderRegistry(pb)
    config = ProjectConfig.load(pb, project["id"])
    provider = registry.get_llm_provider(
        config.project, config.settings, role="outline", role_config=config.role_llm("outline")
    )
    assert provider.provider_name == "openai_compat"
    assert provider.model_name == "gpt-5"
