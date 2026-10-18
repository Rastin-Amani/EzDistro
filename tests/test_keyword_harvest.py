"""Free keyword harvest: the autocomplete source and the research workflow.

No test contacts a real autocomplete endpoint — the provider runs against a mock
transport, and the service runs against the in-memory fake PocketBase.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.api import research as R
from app.providers.base import KeywordIdea
from app.providers.keyword_harvest import AutocompleteHarvester
from app.repositories.jobs import JobRepo
from app.repositories.research import (
    KeywordMetricRepo,
    KeywordRepo,
    ResearchRunRepo,
    ResearchSeedRepo,
)
from app.services.research_keywords import harvest_keywords
from tests.helpers import call_route, make_pb, make_project, make_req, make_user, toast_message
from tests.test_seo_research import _run_job, make_ctx


def _harvest_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host, query = (
            request.url.host,
            (
                request.url.params.get("q")
                or request.url.params.get("query")
                or request.url.params.get("prefix")
                or ""
            ),
        )
        if host == "suggestqueries.google.com":
            if request.url.params.get("ds") == "yt":
                return httpx.Response(200, json=[query, ["gym software youtube"]])
            return httpx.Response(
                200,
                json=[
                    query,
                    [
                        "how much does gym software cost",
                        "gym software price",
                        "totally unrelated recipe",
                    ],
                ],
            )
        if host == "api.bing.com":
            return httpx.Response(200, json=[query, ["gym software comparison"]])
        if host == "duckduckgo.com":
            return httpx.Response(200, json=[{"phrase": "gym software demo"}])
        if host == "completion.amazon.com":
            return httpx.Response(200, json={"suggestions": [{"value": "gym software amazon"}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _harvester(**kwargs) -> tuple[AutocompleteHarvester, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    return AutocompleteHarvester(transport=_harvest_transport(calls), delay=0, **kwargs), calls


def test_harvest_reads_every_source_shape_and_dedups():
    harvester, calls = _harvester()
    ideas = asyncio.run(
        harvester.generate_keyword_ideas(
            keywords=["gym software"], sources=("google", "bing", "duckduckgo", "amazon")
        )
    )
    texts = {idea.text for idea in ideas}
    assert "how much does gym software cost" in texts  # google
    assert "gym software comparison" in texts  # bing osjson
    assert "gym software demo" in texts  # duckduckgo {"phrase"}
    assert "gym software amazon" in texts  # amazon {"value"}
    assert len(ideas) == len(texts)  # deduped
    assert all(idea.avg_monthly_searches == 0 for idea in ideas)  # never invented
    assert all(idea.source.startswith("autocomplete:") for idea in ideas)
    assert harvester.requests == len(calls)
    asyncio.run(harvester.aclose())


def test_harvest_drops_off_topic_suggestions_and_respects_caps():
    harvester, _calls = _harvester(max_requests=3)
    ideas = asyncio.run(
        harvester.generate_keyword_ideas(keywords=["gym software"], sources=("google",))
    )
    assert all("recipe" not in idea.text for idea in ideas)  # shares no token with the seed
    assert harvester.requests <= 3

    capped, _ = _harvester()
    limited = asyncio.run(capped.generate_keyword_ideas(keywords=["gym software"], max_results=1))
    assert len(limited) == 1
    asyncio.run(harvester.aclose())
    asyncio.run(capped.aclose())


class FakeHarvester:
    """Stands in for the autocomplete provider (same method, no network)."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping
        self.requests = 0
        self.failures = 0
        self.by_source: dict[str, int] = {}

    async def generate_keyword_ideas(self, *, keywords: list[str], max_results: int = 0):
        self.requests += 1
        out = [
            KeywordIdea(text=t, source="autocomplete:google")
            for t in self.mapping.get(keywords[0], [])
        ]
        return out[:max_results] if max_results else out


def test_harvest_persists_keywords_and_borrows_existing_metrics():
    from tests.test_seo_research import _setup

    pb, project, registry = _setup()
    job = _run_job(pb, project["id"], key="harvest-1")
    ctx = make_ctx(pb, registry, job)
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="harvest",
        research_type="harvest",
        config={"harvest": {"seeds": ["gym software"]}},
        targeting={"language": "en", "locale": "en-US", "country": "US"},
    )

    # this project already measured one of the harvested keywords
    known = KeywordRepo(pb).ensure_many(
        project=project["id"],
        language="en",
        location_id="US",
        rows=[
            {"normalizedKeyword": "gym software pricing", "displayKeyword": "gym software pricing"}
        ],
    )["gym software pricing"]
    earlier = ResearchRunRepo(pb).create_run(
        project=project["id"], name="earlier", research_type="keywords", config={}, targeting={}
    )
    KeywordMetricRepo(pb).upsert_many(
        [
            {
                "run": earlier["id"],
                "keyword": known["id"],
                "avgMonthlySearches": 2400,
                "competition": "HIGH",
                "competitionIndex": 71,
            }
        ]
    )

    harvester = FakeHarvester(
        {"gym software": ["gym software pricing", "how to choose gym software"]}
    )
    summary = asyncio.run(
        harvest_keywords(
            ctx,
            run_id=run["id"],
            seeds=["gym software"],
            targeting={"language": "en", "locale": "en-US", "country": "US"},
            provider=harvester,
        )
    )
    assert summary["harvested"] == 2 and summary["ideas"] == 2, summary
    assert summary["borrowedMetrics"] == 1, summary

    metrics = KeywordMetricRepo(pb).list_for_run(run["id"])
    by_volume = {int(row["avgMonthlySearches"]): row for row in metrics}
    assert 2400 in by_volume, metrics  # borrowed, not invented
    assert by_volume[2400]["competition"] == "HIGH"
    assert 0 in by_volume  # the unmeasured one stays unmeasured
    stored = {
        row["normalizedKeyword"] for row in KeywordRepo(pb).identity(project["id"], "en", "US")
    }
    assert {"gym software pricing", "how to choose gym software"} <= stored, stored
    assert summary["requests"] == 1


async def test_keywords_export_is_ready_for_a_volume_round_trip():
    """Harvested keywords carry no volume, so the CSV must be paste-ready for
    Keyword Planner's "Get search volume"."""
    from app.repositories.members import MemberRepo
    from app.services.research_keywords import _persist
    from tests.test_seo_research import _setup

    pb, project, registry = _setup()
    MemberRepo(pb).add(project=project["id"], user="u1", role="owner")
    run = ResearchRunRepo(pb).create_run(
        project=project["id"],
        name="harvest",
        research_type="harvest",
        config={"harvest": {"seeds": ["gym software"]}},
        targeting={"language": "en", "locale": "en-US", "country": "US"},
    )
    _persist(
        pb=pb,
        project_id=project["id"],
        run_id=run["id"],
        language="en",
        location_id="US",
        locale="en-US",
        location_name="United States",
        ideas=[KeywordIdea(text="gym software pricing", avg_monthly_searches=2400)],
        source="autocomplete",
    )

    request = make_req(pb, make_user(), project["id"])
    response = R.keywords_export(request, project["id"], run["id"])
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "gym software pricing" in body
    assert "Avg. monthly searches" in body
    assert "2400" in body


def test_harvest_route_queues_a_run_with_seeds_and_no_google_ads():
    pb = make_pb()
    project = make_project(pb, slug="harvest-route", name="H")
    from app.repositories.members import MemberRepo

    MemberRepo(pb).add(project=project["id"], user="u1", role="owner")
    request = make_req(pb, make_user(), project["id"])

    response = call_route(
        R.research_harvest_start,
        request,
        project["id"],
        name="Harvest run",
        seeds="gym software\nmember retention\n",
        max_keywords="400",
    )
    assert "Harvest" in toast_message(response) or "harvest" in toast_message(response)

    runs = ResearchRunRepo(pb)
    run = runs.list_all(filter=f'project="{project["id"]}"')[0]
    assert run["researchType"] == "harvest"
    assert run["config"]["harvest"]["seeds"] == ["gym software", "member retention"]
    assert run["config"]["maxKeywords"] == 400
    assert run["connection"] == "" and run["customerId"] == ""
    assert [row["value"] for row in ResearchSeedRepo(pb).list_for_run(run["id"])] == [
        "gym software",
        "member retention",
    ]
    job = JobRepo(pb).get(run["job"])
    assert job["type"] == "research_run"
    assert job["payload"]["runId"] == run["id"]

    # empty seeds are rejected before anything is created
    empty = call_route(R.research_harvest_start, request, project["id"], seeds="   ")
    assert "seed" in toast_message(empty).lower()
