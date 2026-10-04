"""Tab rendering + connection-aware provider selection.

Pins the model/provider UX fixes:
- the Connections editor exposes a provider select driven by the schema
- AI models / Images selects list the *connected* provider (display name)
- retrieval/chunking/context fields render recommended defaults, never 0
"""

from __future__ import annotations

from app.api import projects as P
from app.repositories.integrations import IntegrationRepo
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import call_route, make_req, make_user


def _setup():
    pb = FakePocketBase(default_unique_fields())
    pb.collection("users").create(
        {"email": "u@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    project = ProjectRepo(pb).create(name="P", slug="proj-a")
    MemberRepo(pb).add(project=project["id"], user="u1", role="owner")
    for category, provider, name, model in (
        ("llm", "openai_compat", "9Router", "gpt-5"),
        ("embedding", "openai_compat", "Embed Gateway", "text-embedding-3-small"),
        ("image", "gemini", "Gemini Images", "gemini-3-pro-image"),
        ("reranker", "cohere_compat", "Cohere", "rerank-v4.0"),
    ):
        IntegrationRepo(pb).create(
            project=project["id"],
            category=category,
            provider=provider,
            display_name=name,
            model=model,
            configuration={"base_url": "https://example.com/v1", "model": model},
            secrets_enc="",
            enabled=True,
        )
    return pb, project


def _render(pb, project, tab: str) -> str:
    req = make_req(pb, make_user(), project["id"])
    resp = call_route(P.project_tab, req, project["id"], tab)
    assert resp.status_code == 200
    return resp.body.decode()


def test_connections_tab_renders_schema_driven_provider_form():
    pb, project = _setup()
    body = _render(pb, project, "integrations")
    assert 'name="provider"' in body  # real select, not a hidden input
    assert "x-data='integrationsEditor(" in body  # provider meta wired in
    assert "9Router" in body


def test_ai_models_tab_lists_connections_by_name():
    pb, project = _setup()
    body = _render(pb, project, "ai_models")
    assert "9Router" in body
    # the option carries the connection's default model for auto-fill
    assert 'data-model="gpt-5"' in body


def test_images_tab_lists_connections_by_name():
    pb, project = _setup()
    body = _render(pb, project, "images")
    assert "Gemini Images" in body
    assert 'data-model="gemini-3-pro-image"' in body


def test_settings_tab_renders_recommended_defaults_not_zero():
    pb, project = _setup()
    body = _render(pb, project, "settings")
    # chunk size / top-k / threshold fall back to documented defaults
    assert 'name="chunk_size"' in body and 'value="500"' in body
    assert 'name="retrieval_top_k"' in body and 'value="20"' in body
    assert 'name="chunk_overlap"' in body and 'value="100"' in body
    assert 'name="embedding_dimensions"' in body and 'value="1536"' in body


def test_registry_resolves_connection_id_for_llm_and_image():
    from app.providers.registry import ProviderRegistry
    from app.services.secrets import SecretsService

    pb, project = _setup()
    registry = ProviderRegistry(pb, secrets=SecretsService(b"0123456789abcdef0123456789abcdef"))
    llm_conn = IntegrationRepo(pb).first(filter=f'project="{project["id"]}" && category="llm"')
    provider = registry.get_llm_provider(
        project,
        {"defaultLlmModel": "gpt-5", "retryPolicy": {}},
        role_config={"provider": llm_conn["id"], "model": "gpt-5"},
    )
    assert provider.provider_name == "openai_compat"
    assert provider.model_name == "gpt-5"

    image_conn = IntegrationRepo(pb).first(filter=f'project="{project["id"]}" && category="image"')
    image = registry.get_image_provider(
        project,
        {},
        integration=image_conn,
        role_config={"provider": image_conn["id"], "model": "gemini-3-pro-image"},
    )
    assert image.provider_name == "gemini"


def test_resume_article_requeues_last_failed_job():
    from app.api import articles as A
    from app.repositories.jobs import JobRepo

    pb, project = _setup()
    topic = TopicRepo(pb).create(project=project["id"], title="T", keyword="k", type="article")
    from app.repositories.articles import ArticleRepo

    article = ArticleRepo(pb).create(
        project=project["id"],
        topic_id=topic["id"],
        title="T",
        slug="t",
    )
    ArticleRepo(pb).set_status(article["id"], "failed")
    job = JobRepo(pb).create(
        project=project["id"],
        type="publish_article",
        payload={"articleId": article["id"], "action": "publish"},
        idempotency_key="publish:failed:1",
        max_attempts=3,
        entity_type="article",
        entity_id=article["id"],
    )
    JobRepo(pb).fail(job["id"], "publish_failed", "boom")

    req = make_req(pb, make_user(), project["id"])
    resp = call_route(A.resume_article, req, project["id"], article["id"])
    assert resp.status_code == 200
    requeued = JobRepo(pb).get(job["id"])
    assert requeued["status"] in ("retrying", "pending")
