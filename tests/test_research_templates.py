"""Render smoke tests for the SEO research templates.

These do not check business logic; they exist so a Jinja syntax error, a macro
signature drift, or a missing context key fails loudly in CI instead of only in
a browser. Every research page/fragment is rendered against the in-memory fake
PocketBase with the same route helpers the other route tests use.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from app.api import projects as P
from app.api import research as R
from app.repositories.jobs import JobRepo
from app.repositories.research import (
    ArticleIdeaRepo,
    ClusterRepo,
    CompetitorPageRepo,
    ContentGapRepo,
    GoogleAdsConnectionRepo,
    KeywordMetricRepo,
    KeywordRepo,
    KeywordVolumeRepo,
    ResearchRunRepo,
    ResearchSeedRepo,
    SerpQueryRepo,
    SerpResultRepo,
)
from tests.helpers import (
    call_route,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_user,
)


def _seed_run(pb, project_id):
    """A completed run with one keyword, cluster, competitor page, gap and idea."""
    runs = ResearchRunRepo(pb)
    run = runs.create_run(
        project=project_id,
        name="Gym software",
        research_type="mixed",
        config={"clustering": "auto", "goal": "grow leads"},
        targeting={"country": "US", "language": "en", "locale": "en-US"},
        fingerprint="fp-1",
    )
    ResearchSeedRepo(pb).replace_for_run(
        run["id"],
        [
            {"seedType": "keyword", "value": "gym software", "normalizedValue": "gym software"},
            {"seedType": "competitor", "value": "rival.com", "normalizedValue": "rival.com"},
        ],
    )
    keyword = KeywordRepo(pb).ensure_many(
        project=project_id,
        language="en",
        location_id="2840",
        rows=[
            {
                "normalizedKeyword": "gym management software",
                "displayKeyword": "gym management software",
                "locale": "en-US",
                "locationName": "United States",
                "source": "google_ads",
            }
        ],
    )["gym management software"]
    metric_row = KeywordMetricRepo(pb).upsert_many(
        [
            {
                "run": run["id"],
                "keyword": keyword["id"],
                "avgMonthlySearches": 8100,
                "competition": "MEDIUM",
                "competitionIndex": 61,
                "averageCpcMicros": 3_250_000,
                "lowTopOfPageBidMicros": 1_200_000,
                "highTopOfPageBidMicros": 6_400_000,
                "currencyCode": "USD",
                "intent": "commercial",
                "intentSource": "deterministic_v1",
            }
        ]
    )
    metric_row = KeywordMetricRepo(pb).list_for_run(run["id"], per_page=5)[0]
    KeywordVolumeRepo(pb).replace_for_keyword(
        run["id"], keyword["id"], [(2026, 1, 8100), (2026, 2, 6600)]
    )
    cluster = ClusterRepo(pb).create(
        {
            "project": project_id,
            "run": run["id"],
            "name": "Gym Management",
            "slug": "gym-management",
            "size": 1,
            "method": "deterministic",
            "primaryKeyword": "gym management software",
            "meta": {"intent": "commercial", "sample": ["gym management software"]},
        }
    )
    KeywordMetricRepo(pb).update(metric_row["id"], {"cluster": cluster["id"]})
    CompetitorPageRepo(pb).create(
        {
            "project": project_id,
            "run": run["id"],
            "domain": "rival.com",
            "url": "https://rival.com/gym-software",
            "canonicalUrl": "https://rival.com/gym-software",
            "title": "Best gym software",
            "contentType": "article",
            "wordCount": 1800,
            "status": "fetched",
            "language": "en",
            "headings": {"h1": ["Best gym software"], "h2": ["Pricing"]},
        }
    )
    ContentGapRepo(pb).replace_for_run(
        run["id"],
        [
            {
                "project": project_id,
                "run": run["id"],
                "gapType": "competitor_only",
                "keyword": "gym billing software",
                "demand": 2400,
                "competitorCoverage": 3,
                "yourCoverage": 0,
                "score": 71,
            }
        ],
    )
    ArticleIdeaRepo(pb).create_many(
        [
            {
                "project": project_id,
                "run": run["id"],
                "title": "How gym membership billing works",
                "primaryKeyword": "gym billing software",
                "secondaryKeywords": ["gym billing"],
                "cluster": cluster["id"],
                "intent": "informational",
                "contentType": "guide",
                "action": "generate",
                "opportunityScore": 72,
                "scoreVersion": "opp_v1",
                "scoreComponents": {"demand": 0.6, "business": 0.8},
                "searchVolume": 2400,
                "googleAdsCompetition": "LOW",
                "confidence": "high",
                "recommendedAngle": "Explain billing cycles for gym owners",
                "evidence": {"source": "ai", "keyword": "gym billing software"},
                "status": "proposed",
            }
        ]
    )
    query = SerpQueryRepo(pb).upsert(
        project=project_id,
        run=run["id"],
        keyword="gym management software",
        provider="serper",
        locale="en-US",
        location="United States",
        device="desktop",
        result_count=2,
        features=["featured_snippet"],
        questions=["What is gym software?"],
        related_searches=["gym app"],
        raw={},
        observed_at=dt.datetime(2026, 9, 30, 10, 0, tzinfo=dt.UTC),
    )
    SerpResultRepo(pb).replace_for_query(
        query["id"],
        [
            {"position": 1, "url": "https://a.com", "domain": "a.com", "title": "A"},
            {"position": 2, "url": "https://b.com", "domain": "b.com", "title": "B"},
        ],
    )
    return run


def _setup():
    pb = make_pb()
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id="u1", role="owner")
    user = make_user()
    run = _seed_run(pb, project["id"])
    return pb, project, run, user


# ---------------------------------------------------------------------------
# Project research tab
# ---------------------------------------------------------------------------
def _call(fn, req, *args, **kwargs):
    """Call a route that may be sync or async."""
    result = fn(req, *args, **kwargs)
    if hasattr(result, "__await__"):
        return asyncio.run(result)
    return result


def test_research_tab_renders_empty():
    pb = make_pb()
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])
    resp = _call(P.project_tab, req, project["id"], "research")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Google Ads is not set up for this project" in body
    assert "No research runs yet" in body


def test_research_tab_renders_with_run():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(P.project_tab, req, project["id"], "research")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Gym software" in body


def test_research_tab_renders_with_a_connection():
    """Regression: `connections` was a dict, so `connections[0]` blew up."""
    pb, project, _, _ = _setup()
    GoogleAdsConnectionRepo(pb).upsert(
        user="u1",
        google_account_id="acct",
        email="me@example.com",
        display_name="Me",
        refresh_token_enc="enc",
        token_metadata={},
        created_by="u1",
    )
    from app.repositories.research import GoogleAdsCustomerRepo

    GoogleAdsCustomerRepo(pb).upsert(
        connection=GoogleAdsConnectionRepo(pb).for_user("u1")["id"],
        customer_id="111",
        descriptive_name="Acme Ads",
        currency_code="USD",
        time_zone="America/New_York",
        is_manager=False,
        accessible=True,
        project=project["id"],
    )
    req = make_req(pb, make_user(), project["id"])
    resp = _call(P.project_tab, req, project["id"], "research")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "me@example.com" in body  # the connection row rendered
    assert "Acme Ads" in body  # the customer option rendered


def test_project_detail_page_renders_research_tab():
    pb, project, _, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    req.query_params = {"tab": "research"}
    resp = _call(P.project_detail, req, project["id"], tab="research")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Research" in body


def test_research_tab_shows_the_google_ads_outcome_banner():
    """The OAuth callback redirects here, so the outcome must be visible."""
    pb, project, _, _ = _setup()
    for ga, expected in (
        ("connected", "Google Ads connected."),
        ("unconfigured", "Google Ads OAuth is not configured for this project yet."),
        ("denied", "Google Ads access was denied."),
        ("error", "Google Ads connection failed. Try again."),
    ):
        req = make_req(pb, make_user(), project["id"])
        req.query_params = {"tab": "research", "ga": ga}
        resp = _call(P.project_detail, req, project["id"], tab="research")
        assert resp.status_code == 200
        assert expected in resp.body.decode()

    # No flag → no banner at all.
    req = make_req(pb, make_user(), project["id"])
    req.query_params = {"tab": "research"}
    resp = _call(P.project_detail, req, project["id"], tab="research")
    assert "Google Ads connected." not in resp.body.decode()


# ---------------------------------------------------------------------------
# Run page + fragments
# ---------------------------------------------------------------------------
def test_run_page_renders():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_run_page, req, project["id"], run["id"])
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Gym software" in body
    assert "Keywords" in body


def test_run_status_fragment_renders():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_status, req, project["id"], run["id"])
    assert resp.status_code == 200
    assert "research-status" in resp.body.decode()


def test_every_tab_fragment_renders():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    for tab in ("keywords", "serp", "competitors", "clusters", "gaps", "opportunities"):
        resp = _call(R.research_tab, req, project["id"], run["id"], tab)
        body = resp.body.decode()
        assert resp.status_code == 200, tab
        assert body.strip(), tab


def test_unknown_tab_fragment_is_empty():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_tab, req, project["id"], run["id"], "nope")
    assert resp.status_code == 200
    assert resp.body.decode() == ""


def test_keyword_detail_fragment_renders():
    pb, project, run, _ = _setup()
    metric = KeywordMetricRepo(pb).list_for_run(run["id"], per_page=5)[0]
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.keyword_detail, req, project["id"], run["id"], metric["id"])
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "gym management software" in body
    # the honesty note must survive: Ads competition is not organic difficulty
    assert "not organic ranking difficulty" in body


def test_run_page_not_found_renders_not_found_page():
    pb, project, _, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_run_page, req, project["id"], "does-not-exist")
    assert resp.status_code == 200
    assert "not found" in resp.body.decode().lower()


def test_run_page_rejects_other_project():
    pb, project, run, _ = _setup()
    other = make_project(pb, slug="proj-b", name="B")
    make_member(pb, other["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), other["id"])
    resp = _call(R.research_run_page, req, other["id"], run["id"])
    assert resp.status_code == 200
    assert "not found" in resp.body.decode().lower()


# ---------------------------------------------------------------------------
# Start route — validation + happy path (job is created, nothing runs inline)
# ---------------------------------------------------------------------------
def test_start_requires_a_seed():
    pb, project, _, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(
        R.research_start,
        req,
        project["id"],
        name="",
        keywords="",
        site="",
        urls="",
        competitors="",
        country="US",
        language="en",
        locale="",
        network="GOOGLE_SEARCH",
        include_adult="",
        clustering="auto",
        goal="",
        connection_id="",
        customer_id="",
        max_keywords="",
        max_competitor_pages="",
        max_serp_queries="",
        refresh="",
    )
    assert resp.status_code == 200
    assert "toast" in str(resp.headers).lower() or "add at least one" in str(resp.headers).lower()


def test_start_with_only_competitors_queues_a_run_and_job():
    pb, project, _, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    resp = _call(
        R.research_start,
        req,
        project["id"],
        name="Competitor scan",
        keywords="",
        site="",
        urls="",
        competitors="rival.com\nother.com",
        country="US",
        language="en",
        locale="en-US",
        network="GOOGLE_SEARCH",
        include_adult="",
        clustering="deterministic",
        goal="find gaps",
        connection_id="",
        customer_id="",
        max_keywords="",
        max_competitor_pages="10",
        max_serp_queries="",
        refresh="",
    )
    assert resp.status_code == 200
    runs = ResearchRunRepo(pb).list_for_project(project["id"])
    assert len(runs) == 2  # the seeded run + the new one
    created = [r for r in runs if r["name"] == "Competitor scan"][0]
    assert ResearchSeedRepo(pb).list_for_run(created["id"], "competitor")
    job = JobRepo(pb).first(filter=f'entityId="{created["id"]}"')
    assert job is not None
    assert job["type"] == "research_run"


def test_start_reuses_an_identical_run_without_force():
    pb, project, run, _ = _setup()
    req = make_req(pb, make_user(), project["id"])
    before = ResearchRunRepo(pb).count_for_project(project["id"])
    resp = _call(
        R.research_start,
        req,
        project["id"],
        name="Same question",
        keywords="gym software",
        site="",
        urls="",
        competitors="rival.com",
        country="US",
        language="en",
        locale="en-US",
        network="GOOGLE_SEARCH",
        include_adult="",
        clustering="auto",
        goal="grow leads",
        connection_id="",
        customer_id="",
        max_keywords="",
        max_competitor_pages="",
        max_serp_queries="",
        refresh="",
    )
    assert resp.status_code == 200
    after = ResearchRunRepo(pb).count_for_project(project["id"])
    assert after == before  # identical to the seeded run's targeting+seeds


def test_google_ads_connect_redirects_to_connect_when_unconfigured():
    pb = make_pb()
    user = make_user()
    req = make_req(pb, user, "")
    resp = _call(R.google_ads_connect, req, project_id="")
    assert resp.status_code == 303
    # No project in the URL → root, with the outcome flag as a real query string.
    assert resp.headers["location"] == "/?ga=unconfigured"


def test_opportunity_accept_creates_an_article_and_a_write_job():
    pb, project, run, _ = _setup()
    idea = ArticleIdeaRepo(pb).all_for_run(run["id"])[0]
    made = GoogleAdsConnectionRepo  # imported for parity with the real route
    assert made is not None
    before = len(pb.collection("jobs").get_full_list())
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.opportunity_action, req, project["id"], run["id"], idea["id"], action="accept")
    assert resp.status_code == 200
    refreshed = ArticleIdeaRepo(pb).get(idea["id"])
    assert refreshed["status"] == "accepted"
    assert refreshed["article"]
    jobs = JobRepo(pb).list_for_project(project["id"], per_page=50)
    assert any(j["type"] == "write_article" for j in jobs)
    assert len(pb.collection("jobs").get_full_list()) == before + 1


def test_resume_failed_run_queues_a_new_job_and_clears_the_error():
    pb, project, run, _ = _setup()
    ResearchRunRepo(pb).set_status(
        run["id"],
        "failed",
        error_code="PermanentError",
        error_message="boom at the last step",
    )
    before = len(pb.collection("jobs").get_full_list())
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_resume, req, project["id"], run["id"])
    assert resp.status_code == 200
    refreshed = ResearchRunRepo(pb).get(run["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["errorMessage"] == ""
    assert refreshed["errorCode"] == ""
    assert len(pb.collection("jobs").get_full_list()) == before + 1


def test_resume_running_run_is_rejected():
    pb, project, run, _ = _setup()
    ResearchRunRepo(pb).set_status(run["id"], "running")
    before = len(pb.collection("jobs").get_full_list())
    req = make_req(pb, make_user(), project["id"])
    resp = _call(R.research_resume, req, project["id"], run["id"])
    assert resp.status_code == 200
    assert "already in progress" in resp.headers.get("hx-trigger", "")
    assert len(pb.collection("jobs").get_full_list()) == before


def test_stage_tracker_skips_completed_stages_on_resume():
    """The whole point of resume: a run that died at the last step re-runs only
    the stages it never recorded."""
    import asyncio
    from types import SimpleNamespace

    from app.services.research import StageTracker

    pb, project, run, _ = _setup()
    ResearchRunRepo(pb).set_stage(
        run["id"], "validate", progress=5, stage_state={"validate": {"seeds": 1}}
    )
    ctx = SimpleNamespace(
        pb=pb,
        stage_started=lambda *a, **k: None,
        stage_completed=lambda *a, **k: None,
        progress=lambda *a, **k: None,
    )
    stages = StageTracker(ctx, run["id"])
    assert stages.done("validate") is True
    assert stages.done("opportunities") is False
    forced = StageTracker(ctx, run["id"], force=True)
    assert forced.done("validate") is False
