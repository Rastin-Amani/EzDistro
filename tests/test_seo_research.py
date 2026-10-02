"""SEO research engine business logic: keyword collection, WordPress mirror,
clustering, content gaps, opportunity generation and the orchestrator.

Everything runs on the in-memory fake PocketBase and fake providers — no live
Google Ads, WordPress, SERP or LLM service is ever contacted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import KeywordIdea, PermanentError, WPPost
from app.providers.serp import NoneSERPProvider
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.research import (
    ArticleIdeaRepo,
    GoogleAdsConnectionRepo,
    KeywordMetricRepo,
    KeywordRepo,
    ResearchRunRepo,
    ResearchSeedRepo,
)
from app.services import google_ads as google_ads_service
from app.services import research as orchestrator
from app.services.research_clustering import cluster_run
from app.services.research_keywords import collect_keywords, merge_ideas, plan_batches
from app.services.research_opportunities import (
    generate_opportunities,
    parse_goal,
    score_opportunity,
)
from app.services.serp import collect_serp, serp_available
from app.services.settings import ProjectConfig
from app.services.wordpress_sync import sync_posts
from tests.fake_providers import FakeLLM, FakePublisher, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import make_project

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeAds:
    """A scriptable Google Ads client (GAQL + keyword ideas)."""

    def __init__(self, ideas: list[KeywordIdea] | None = None) -> None:
        self.ideas = ideas or [
            KeywordIdea(
                "gym management software",
                8100,
                "MEDIUM",
                61,
                3_250_000,
                1_000_000,
                5_000_000,
                "USD",
                [(2026, 8, 8100), (2026, 7, 7900)],
            )
        ]
        self.idea_calls: list[dict[str, Any]] = []
        self.closed = False

    async def search(self, query: str, customer_id: str | None = None) -> list[dict[str, Any]]:
        if "language_constant" in query:
            return [{"languageConstant": {"id": "1000", "code": "en"}}]
        if "geo_target_constant" in query:
            return [
                {
                    "geoTargetConstant": {
                        "id": "2840",
                        "name": "United States",
                        "countryCode": "US",
                    }
                }
            ]
        return []

    async def generate_keyword_ideas(self, **kwargs: Any) -> list[KeywordIdea]:
        self.idea_calls.append(kwargs)
        return list(self.ideas)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )


def _run_job(pb: FakePocketBase, project_id: str, *, key: str) -> dict[str, Any]:
    return JobRepo(pb).create(
        project=project_id, type="research_run", payload={}, idempotency_key=key
    )


def _setup(reg: FakeRegistry | None = None) -> tuple[FakePocketBase, dict, FakeRegistry]:
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = reg or FakeRegistry()
    return pb, project, registry


# ---------------------------------------------------------------------------
# Keyword collection
# ---------------------------------------------------------------------------
def test_plan_batches_splits_keywords_and_single_seeds():
    seeds = [{"seedType": "keyword", "value": f"kw {i}"} for i in range(45)]
    seeds.append({"seedType": "url", "value": "example.com/features"})
    seeds.append({"seedType": "site", "value": "competitor.com"})
    batches = plan_batches(seeds)
    keyword_batches = [b for b in batches if b["kind"] == "keyword"]
    assert len(keyword_batches) == 3  # 45 keywords / 20 per batch
    kinds = {b["kind"] for b in batches}
    assert "url" in kinds and "site" in kinds
    url_batch = next(b for b in batches if b["kind"] == "url")
    assert url_batch["url"].startswith("https://")


def test_merge_ideas_keeps_the_higher_volume():
    store: dict[str, Any] = {}
    merge_ideas(store, [KeywordIdea("Gym Software", 100)])
    merged = merge_ideas(store, [KeywordIdea("gym  software", 500)])
    assert len(merged) == 1
    assert merged[0].avg_monthly_searches == 500


def test_collect_keywords_persists_and_is_idempotent():
    pb, project, registry = _setup()
    job = _run_job(pb, project["id"], key="kw-1")
    ctx = make_ctx(pb, registry, job)
    ads = FakeAds()
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    targeting = {"country": "US", "language": "en", "locale": "en-US"}
    seeds = [{"seedType": "keyword", "value": "gym management software"}]

    first = asyncio.run(
        collect_keywords(
            ctx, provider=ads, run_id=run["id"], customer_id="123", targeting=targeting, seeds=seeds
        )
    )
    assert first["ideas"] == 1
    assert first["languageId"] == "languageConstants/1000"
    assert KeywordMetricRepo(pb).count_for_run(run["id"]) == 1
    assert len(pb.collection("keyword_volumes").get_full_list()) == 2

    # Running again must not duplicate keyword rows (identity is normalized keyword).
    asyncio.run(
        collect_keywords(
            ctx, provider=ads, run_id=run["id"], customer_id="123", targeting=targeting, seeds=seeds
        )
    )
    assert len(pb.collection("keywords").get_full_list()) == 1


# ---------------------------------------------------------------------------
# WordPress mirror
# ---------------------------------------------------------------------------
def test_wordpress_sync_mirrors_creates_and_flags_changes():
    posts = [
        WPPost(
            11, "Yoga Studio Software", "<h1>Yoga</h1><p>one</p>", "https://s.test/yoga", "publish"
        ),
        WPPost(12, "Gym CRM", "<h1>CRM</h1><p>two</p>", "https://s.test/crm", "publish"),
    ]
    pb, project, registry = _setup()
    registry.publisher = FakePublisher(posts=posts)
    job = _run_job(pb, project["id"], key="wp-1")
    ctx = make_ctx(pb, registry, job)

    first = asyncio.run(sync_posts(ctx, registry.publisher))
    assert first["created"] == 2
    rows = pb.collection("articles").get_full_list()
    assert {r["source"] for r in rows} == {"wordpress"}
    assert {r["wordpressPostId"] for r in rows} == {11, 12}
    assert {r["syncStatus"] for r in rows} == {"synced"}

    # No changes → nothing created or updated.
    assert asyncio.run(sync_posts(ctx, registry.publisher))["created"] == 0

    # Remote edit → updated; a locally-worked article is flagged, not overwritten.
    posts[0].content_html = "<h1>Yoga</h1><p>one changed</p>"
    posts[0].modified = "2026-09-30T10:00:00"
    posts[1].content_html = "<h1>CRM</h1><p>two changed</p>"
    posts[1].modified = "2026-09-30T10:00:00"
    yoga = next(r for r in rows if r["wordpressPostId"] == 11)
    pb.collection("articles").update(yoga["id"], {"status": "review"})
    second = asyncio.run(sync_posts(ctx, registry.publisher))
    assert second["flagged"] == 1  # yoga is being edited locally
    assert second["updated"] == 1  # gym crm was safe to refresh


def test_wordpress_sync_flags_remote_deletions():
    pb, project, registry = _setup()
    registry.publisher = FakePublisher(
        posts=[WPPost(21, "T", "<p>t</p>", "https://s.test/t", "publish")]
    )
    job = _run_job(pb, project["id"], key="wp-2")
    ctx = make_ctx(pb, registry, job)
    asyncio.run(sync_posts(ctx, registry.publisher))
    registry.publisher.posts = []
    result = asyncio.run(sync_posts(ctx, registry.publisher))
    assert result["deleted"] == 1
    row = pb.collection("articles").get_full_list()[0]
    assert row["syncStatus"] == "remote_deleted"


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def _seed_keywords(pb, project_id: str, run_id: str, keywords: list[tuple[str, int]]) -> None:
    rows = KeywordRepo(pb).ensure_many(
        project=project_id,
        language="en",
        location_id="2840",
        rows=[{"normalizedKeyword": text, "displayKeyword": text} for text, _ in keywords],
    )
    KeywordMetricRepo(pb).upsert_many(
        [
            {"run": run_id, "keyword": rows[text]["id"], "avgMonthlySearches": volume}
            for text, volume in keywords
        ]
    )


def test_deterministic_clustering_is_idempotent_and_splits_intents():
    pb, project, registry = _setup()
    job = _run_job(pb, project["id"], key="cl-1")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    _seed_keywords(
        pb,
        project["id"],
        run["id"],
        [
            ("gym management software", 8100),
            ("best gym management software", 2400),
            ("gym management software pricing", 320),
        ],
    )
    first = asyncio.run(cluster_run(ctx, run_id=run["id"], method="deterministic"))
    assert first["clusters"] == 2  # pricing query is its own cluster
    assert all(m.get("cluster") for m in KeywordMetricRepo(pb).list_for_run(run["id"], per_page=50))
    assert all(m.get("intent") for m in KeywordMetricRepo(pb).list_for_run(run["id"], per_page=50))

    second = asyncio.run(cluster_run(ctx, run_id=run["id"], method="deterministic"))
    assert second["assigned"] == 0  # nothing moved on re-run
    assert second["removed"] == 0


def test_llm_clustering_falls_back_on_garbage_output():
    pb, project, registry = _setup()
    registry.llm = FakeLLM(["not json at all"])
    job = _run_job(pb, project["id"], key="cl-2")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    _seed_keywords(pb, project["id"], run["id"], [("gym software", 100), ("yoga software", 90)])
    result = asyncio.run(cluster_run(ctx, run_id=run["id"], method="llm"))
    assert result["method"] == "mixed"  # fell back to deterministic for the bad batch
    assert result["clusters"] >= 1


# ---------------------------------------------------------------------------
# Optional SERP
# ---------------------------------------------------------------------------
def test_no_serp_provider_is_first_class():
    assert serp_available(NoneSERPProvider()) is False
    pb, project, registry = _setup()
    job = _run_job(pb, project["id"], key="serp-1")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    result = asyncio.run(
        collect_serp(
            ctx, provider=NoneSERPProvider(), run_id=run["id"], keywords=["a", "b"], locale="en-US"
        )
    )
    assert result["available"] is False
    assert result["collected"] == 0


# ---------------------------------------------------------------------------
# Goal + scoring
# ---------------------------------------------------------------------------
def test_goal_parsing_and_score_monotonicity():
    goal = parse_goal(
        "Get qualified US leads, at least 200 searches, max 300 ideas, avoid pricing pages"
    )
    assert goal.min_volume == 200
    assert goal.max_opportunities == 300
    # a lead-generation goal raises the intent bar above pure information
    assert set(goal.priority_intents) - {"informational"}
    assert goal.allows("Gym management software") is True
    assert goal.allows("Pricing page guide") is False

    high, detail = score_opportunity(
        volume=8100, intent="commercial", action="generate", content_type="guide", goal=goal
    )
    low, _ = score_opportunity(
        volume=10, intent="informational", action="reject", content_type="glossary", goal=goal
    )
    assert high > low
    assert detail["serp"] is None  # no SERP signals are invented
    assert detail["version"] == "opp_v1"
    assert detail["version"] == "opp_v1"


# ---------------------------------------------------------------------------
# Opportunity generation
# ---------------------------------------------------------------------------
def test_generate_opportunities_skips_invented_keywords_and_merges_dupes():
    payload = {
        "opportunities": [
            {
                "title": "How Gym Membership Billing Works",
                "primary_keyword": "gym management software",
                "intent": "informational",
                "content_type": "guide",
                "action": "generate",
                "angle": "explain billing",
                "unique_value": "first-hand",
                "brief": "practical guide",
            },
            {  # primary keyword was never collected → must be dropped
                "title": "Invented Page",
                "primary_keyword": "totally invented phrase xyz",
                "intent": "commercial",
                "content_type": "guide",
                "action": "generate",
            },
            {  # same concept as #1 → merged, not persisted twice
                "title": "How Gym Membership Billing Works",
                "primary_keyword": "gym management software",
                "intent": "informational",
                "content_type": "guide",
                "action": "generate",
            },
        ]
    }
    pb, project, registry = _setup()
    registry.llm = FakeLLM([json.dumps(payload)])
    job = _run_job(pb, project["id"], key="opp-1")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    _seed_keywords(pb, project["id"], run["id"], [("gym management software", 8100)])
    asyncio.run(cluster_run(ctx, run_id=run["id"], method="deterministic"))

    result = asyncio.run(
        generate_opportunities(ctx, run_id=run["id"], goal_text="Get qualified US leads")
    )
    assert result["created"] == 1
    assert result["duplicates"] == 1
    assert result["skipped"] == 1
    ideas = ArticleIdeaRepo(pb).all_for_run(run["id"])
    assert len(ideas) == 1
    assert ideas[0]["searchVolume"] == 8100
    assert ideas[0]["status"] == "proposed"


def test_cannibalization_forces_update_action():
    pb, project, registry = _setup()
    registry.llm = FakeLLM(
        [
            json.dumps(
                {
                    "opportunities": [
                        {
                            "title": "Best Gym Software",
                            "primary_keyword": "gym management software",
                            "intent": "commercial",
                            "content_type": "best-of list",
                            "action": "generate",
                            "angle": "a",
                            "unique_value": "u",
                            "brief": "b",
                        }
                    ]
                }
            )
        ]
    )
    job = _run_job(pb, project["id"], key="opp-2")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="r",
        research_type="keywords",
        config={},
        targeting={},
        fingerprint="fp",
    )
    _seed_keywords(pb, project["id"], run["id"], [("gym management software", 8100)])
    # An existing article already owns this topic.
    topic = pb.collection("topics").create({"project": project["id"], "title": "Best Gym Software"})
    pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "Best Gym Software",
            "status": "published",
        }
    )
    asyncio.run(cluster_run(ctx, run_id=run["id"], method="deterministic"))
    asyncio.run(generate_opportunities(ctx, run_id=run["id"], goal_text="grow"))
    ideas = ArticleIdeaRepo(pb).all_for_run(run["id"])
    assert ideas and ideas[0]["action"] == "update"  # a new URL is NOT created


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _make_run(pb, project, *, config: dict, connection: str = "", customer: str = "") -> dict:
    return ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="Full run",
        research_type="mixed",
        config=config,
        targeting={"country": "US", "language": "en", "locale": "en-US"},
        fingerprint="fp",
        connection=connection,
        customer_id=customer,
    )


def test_research_pipeline_runs_all_stages_and_resumes():
    pb, project, registry = _setup()
    registry.llm = FakeLLM(
        [
            json.dumps(
                {
                    "opportunities": [
                        {
                            "title": "Gym Software Guide",
                            "primary_keyword": "gym management software",
                            "intent": "commercial",
                            "content_type": "guide",
                            "action": "generate",
                            "angle": "a",
                            "unique_value": "u",
                            "brief": "b",
                        }
                    ]
                }
            )
        ]
    )
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                5, "Yoga Studio Software", "<h1>Yoga</h1><p>c</p>", "https://s.test/y", "publish"
            )
        ]
    )
    job = _run_job(pb, project["id"], key="pipe-1")
    ctx = make_ctx(pb, registry, job)
    conn = GoogleAdsConnectionRepo(pb).upsert(
        user="u1", google_account_id="g", email="e", display_name="d", refresh_token_enc="enc"
    )
    run = _make_run(
        pb,
        project,
        config={"clustering": "deterministic", "goal": "grow"},
        connection=conn["id"],
        customer="123",
    )
    ResearchSeedRepo(pb).replace_for_run(
        run["id"],
        [
            {
                "seedType": "keyword",
                "value": "gym management software",
                "normalizedValue": "gym management software",
            }
        ],
    )

    ads = FakeAds()
    with mock.patch.object(orchestrator, "client_for_connection", return_value=ads):
        out = asyncio.run(
            orchestrator.research_pipeline(ctx, run=ResearchRunRepo(pb).get(run["id"]))
        )

    assert set(out["stages"]) == {
        "validate",
        "wordpress_sync",
        "keyword_collection",
        "competitor_crawl",
        "serp",
        "clustering",
        "gaps",
        "opportunities",
        "finalize",
    }
    stored = ResearchRunRepo(pb).get(run["id"])
    assert stored["status"] == "completed"
    assert stored["currentStage"] == "finalize"
    assert ads.closed is True  # the Ads client is always released

    # A second pass must reuse the cached stage summaries (no new Ads calls).
    calls_before = len(ads.idea_calls)
    with mock.patch.object(orchestrator, "client_for_connection", return_value=ads):
        asyncio.run(orchestrator.research_pipeline(ctx, run=ResearchRunRepo(pb).get(run["id"])))
    assert len(ads.idea_calls) == calls_before


def test_pipeline_fails_without_a_connection_for_keyword_seeds():
    pb, project, registry = _setup()
    job = _run_job(pb, project["id"], key="pipe-2")
    ctx = make_ctx(pb, registry, job)
    run = _make_run(pb, project, config={})  # no connection / customer
    ResearchSeedRepo(pb).replace_for_run(
        run["id"],
        [{"seedType": "keyword", "value": "gym software", "normalizedValue": "gym software"}],
    )
    try:
        asyncio.run(orchestrator.research_pipeline(ctx, run=ResearchRunRepo(pb).get(run["id"])))
    except PermanentError as exc:
        assert "Google Ads" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("expected a PermanentError for keyword seeds without a connection")


# ---------------------------------------------------------------------------
# Google Ads credentials come from the project's Connections tab
# ---------------------------------------------------------------------------
def _google_ads_integration(pb, project_id: str, *, client_id: str, client_secret: str) -> dict:
    from app.services.secrets import get_secrets_service

    return IntegrationRepo(pb).create(
        project=project_id,
        category="google_ads",
        provider="google_ads",
        display_name="Google Ads",
        configuration={
            "client_id": client_id,
            "redirect_uri": "https://app.test/projects/google-ads/callback",
            "api_version": "v25",
            "login_customer_id": "",
            "masked": "",
        },
        secrets_enc=get_secrets_service().encrypt(json.dumps({"client_secret": client_secret})),
        enabled=True,
        created_by="u1",
    )


def test_google_ads_credentials_resolve_from_the_project_integration():
    pb, project, _registry = _setup()
    # No env credentials are configured in tests (settings.google_ads_client_id == "").
    assert google_ads_service.client_configured(pb, project["id"]) is False

    _google_ads_integration(
        pb, project["id"], client_id="cid.apps.googleusercontent.com", client_secret="shh"
    )

    # The project-scoped integration alone makes Google Ads configured.
    assert google_ads_service.client_configured(pb, project["id"]) is True
    # An unrelated project stays unconfigured.
    other = make_project(pb, slug="proj-b", name="B")
    assert google_ads_service.client_configured(pb, other["id"]) is False


def test_google_ads_authorize_url_requires_project_credentials():
    pb, project, _registry = _setup()
    state = google_ads_service.make_state(user_id="u1", project_id=project["id"])
    # No integration and no env creds → a clear, per-project error.
    try:
        google_ads_service.authorize_url(
            state, request_base="https://app.test/", pb=pb, project_id=project["id"]
        )
    except PermanentError as exc:
        assert "Google Ads" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("expected a PermanentError before a Google Ads connection exists")

    _google_ads_integration(
        pb, project["id"], client_id="cid.apps.googleusercontent.com", client_secret="shh"
    )
    url = google_ads_service.authorize_url(
        state, request_base="https://app.test/", pb=pb, project_id=project["id"]
    )
    assert "cid.apps.googleusercontent.com" in url
    assert "accounts.google.com" in url


def test_google_ads_callback_is_served_on_both_paths():
    """Google may redirect to either registered URI; both must resolve."""
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/auth/google-ads/callback" in paths
    assert "/projects/google-ads/callback" in paths


def test_google_ads_redirect_uri_falls_back_to_auth_path():
    pb, project, _registry = _setup()
    # No connection → the derived fallback (the /auth form Google Cloud expects).
    fallback = google_ads_service.redirect_uri("https://app.test/", pb=pb, project_id=project["id"])
    assert fallback == "https://app.test/auth/google-ads/callback"

    # A configured redirect_uri on the connection is used verbatim.
    _google_ads_integration(
        pb, project["id"], client_id="cid.apps.googleusercontent.com", client_secret="shh"
    )
    configured = google_ads_service.redirect_uri(
        "https://app.test/", pb=pb, project_id=project["id"]
    )
    assert configured == "https://app.test/projects/google-ads/callback"


def test_ga_flag_uses_the_right_separator():
    """A bare target needs `?`, a target with a query string needs `&`."""
    from app.api.research import _flagged

    # No project in the OAuth state → the callback redirects to `/`.
    # `/&ga=error` is a path segment and 404s; it must be `/?ga=error`.
    assert _flagged("/", "error") == "/?ga=error"
    assert _flagged("/", "connected") == "/?ga=connected"
    # A known project already has a query string.
    assert _flagged("/projects/p1?tab=research", "connected") == (
        "/projects/p1?tab=research&ga=connected"
    )
    # Never double up a separator.
    assert _flagged("/projects/p1?", "connected") == "/projects/p1?ga=connected"
    assert _flagged("/projects/p1?tab=research&", "denied") == (
        "/projects/p1?tab=research&ga=denied"
    )


def test_google_ads_state_round_trips_user_and_project():
    """The callback reads state["u"]/state["p"] — make_state must store them."""
    state = google_ads_service.make_state(user_id="u1", project_id="proj-a")
    payload = google_ads_service.read_state(state)
    assert payload["u"] == "u1"
    assert payload["p"] == "proj-a"


def test_google_ads_callback_redirects_to_the_project_after_connect():
    """A completed OAuth callback must land back on the run's project, not `/`.

    Regression: the callback once read state keys that did not exist, so it
    always redirected to `/` and stored nothing.
    """
    import asyncio

    from app.api import research as R
    from tests.helpers import make_req, make_user

    pb, project, _registry = _setup()
    state = google_ads_service.make_state(user_id="u1", project_id=project["id"])

    captured: dict[str, object] = {}

    async def fake_connect(pb_, *, user_id, code, request_base="", project_id=""):
        captured.update(user_id=user_id, code=code, project_id=project_id)
        return {"id": "conn1"}

    req = make_req(pb, make_user(), project["id"])
    req.base_url = "https://app.test/"  # callbacks build the redirect from this
    original = google_ads_service.connect
    google_ads_service.connect = fake_connect  # type: ignore[assignment]
    try:
        resp = asyncio.run(R.google_ads_callback(req, code="auth-code", state=state, error=""))
    finally:
        google_ads_service.connect = original  # type: ignore[assignment]

    assert resp.status_code == 303
    assert resp.headers["location"] == (f"/projects/{project['id']}?tab=research&ga=connected")
    assert captured["project_id"] == project["id"]
    assert captured["user_id"] == "u1"


def test_google_ads_discover_customers_stores_rows_for_the_project():
    """After OAuth, discovering customers must populate google_ads_customers.

    Regression: nothing was ever stored because the callback had no user id.
    """
    import asyncio

    from app.providers.base import AdsCustomer
    from app.repositories.research import GoogleAdsConnectionRepo, GoogleAdsCustomerRepo
    from app.services import google_ads as g

    pb, project, _registry = _setup()
    connection = GoogleAdsConnectionRepo(pb).upsert(
        user="u1",
        google_account_id="acct",
        email="me@example.com",
        display_name="Me",
        refresh_token_enc="enc",
        token_metadata={},
        created_by="u1",
    )

    class StubClient:
        async def list_customers(self, customer_id=None):
            return [
                AdsCustomer("111", "Acme Ads", "USD", "America/New_York"),
                AdsCustomer("222", "Beta Ads", "EUR", "Europe/Berlin"),
            ]

        async def aclose(self):
            return None

    original = g.client_for_connection
    g.client_for_connection = lambda *a, **k: StubClient()  # type: ignore[assignment]
    try:
        rows = asyncio.run(g.discover_customers(pb, connection, project_id=project["id"]))
    finally:
        g.client_for_connection = original  # type: ignore[assignment]

    assert {r["customerId"] for r in rows} == {"111", "222"}
    stored = GoogleAdsCustomerRepo(pb).list_for_project(project["id"])
    assert {r["customerId"] for r in stored} == {"111", "222"}


def test_google_ads_list_customers_keeps_account_when_metadata_probe_fails():
    """A denied customer_client probe must not drop the accessible account.

    Regression: one failing metadata lookup aborted the whole listing, so a
    single direct Ads account produced zero customers.
    """
    import asyncio

    from app.providers.base import AdsCustomer, PermanentError
    from app.providers.google_ads import GoogleAdsClient

    client = GoogleAdsClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
    )

    async def fake_accessible() -> list[str]:
        return ["1234567890"]

    async def boom(customer_id: str):
        raise PermanentError("PERMISSION_DENIED")

    client.list_accessible_customers = fake_accessible  # type: ignore[assignment]
    client._customer_metadata = boom  # type: ignore[assignment]

    rows = asyncio.run(client.list_customers())
    assert [c.customer_id for c in rows] == ["1234567890"]
    assert isinstance(rows[0], AdsCustomer)
    asyncio.run(client.aclose())


def test_google_ads_list_customers_dedupes_metadata_rows():
    """Metadata rows are merged without duplicating the probed customer id."""
    import asyncio

    from app.providers.base import AdsCustomer
    from app.providers.google_ads import GoogleAdsClient

    client = GoogleAdsClient(client_id="cid", client_secret="secret", refresh_token="r")

    async def fake_accessible() -> list[str]:
        return ["111", "111"]

    async def meta(customer_id: str):
        return [AdsCustomer(customer_id=customer_id, descriptive_name="Acme")]

    client.list_accessible_customers = fake_accessible  # type: ignore[assignment]
    client._customer_metadata = meta  # type: ignore[assignment]

    rows = asyncio.run(client.list_customers())
    assert [c.customer_id for c in rows] == ["111"]
    asyncio.run(client.aclose())


def test_google_ads_unauthenticated_error_is_actionable():
    """UNAUTHENTICATED must point at developer token / API access, not 'expired'.

    Regression: the message told users to reconnect, which can never fix a
    missing developer token or an unapproved Google Cloud project.
    """
    import httpx
    import pytest

    from app.providers.base import PermanentError
    from app.providers.google_ads import GoogleAdsClient

    client = GoogleAdsClient(client_id="cid", client_secret="secret", refresh_token="r")
    response = httpx.Response(
        401,
        json={
            "error": {
                "code": 401,
                "message": "Request had invalid authentication credentials.",
                "status": "UNAUTHENTICATED",
                "details": [
                    {
                        "errors": [
                            {
                                "errorCode": {
                                    "authenticationError": "DEVELOPER_TOKEN_NOT_APPROVED"
                                },
                                "message": "The developer token is not approved.",
                            }
                        ]
                    }
                ],
            }
        },
        request=httpx.Request("GET", "https://googleads.googleapis.com/v25/x"),
    )
    with pytest.raises(PermanentError) as caught:
        client._classify(response, "listAccessibleCustomers")
    message = str(caught.value)
    assert "developer token" in message.lower()
    assert "expired" not in message.lower()
    assert caught.value.details["google_status"] == "UNAUTHENTICATED"
    assert caught.value.details["google_error_code"] == "DEVELOPER_TOKEN_NOT_APPROVED"
    assert "not approved" in caught.value.details["google_message"]
