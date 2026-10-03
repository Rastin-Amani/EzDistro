"""Keyword Planner file import: parsing, column detection, dedupe, flow."""

from __future__ import annotations

import asyncio
import io
import zipfile

from app.domain.keywords import normalize_keyword
from app.repositories.research import KeywordMetricRepo, ResearchRunRepo
from app.services.research_import import parse_keyword_file

CSV_EXPORT = (
    b"Keyword,Currency,Avg. monthly searches,Competition,Competition (indexed value),"
    b"Top of page bid (low range),Top of page bid (high range)\n"
    b'gym management software,USD,"8,100",Medium,61,1.50,4.20\n'
    b'gym crm,USD,"1,200",Low,12,0.80,2.00\n'
    b'Gym Management Software,USD,"8,100",Medium,61,1.50,4.20\n'
    b"gym software pricing,USD,320,High,80,2.10,6.00\n"
)


def _xlsx_bytes() -> bytes:
    shared = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" count="6" uniqueCount="6">'
        "<si><t>Keyword</t></si>"
        "<si><t>Avg. monthly searches</t></si>"
        "<si><t>Competition</t></si>"
        "<si><t>gym management software</t></si>"
        "<si><t>gym crm</t></si>"
        "<si><t>Medium</t></si>"
        "</sst>"
    )
    sheet = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>8100</v></c>'
        '<c r="C2" t="s"><v>5</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>4</v></c><c r="B3"><v>1200</v></c>'
        '<c r="C3" t="s"><v>5</v></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_parse_csv_detects_columns_and_dedupes():
    ideas, report = parse_keyword_file(CSV_EXPORT, "export.csv")
    assert report["ok"] is True
    assert report["format"] == "csv"
    assert report["ideas"] == 3  # case-insensitive duplicate collapsed
    by_key = {normalize_keyword(i.text): i for i in ideas}
    assert by_key["gym management software"].avg_monthly_searches == 8100
    assert by_key["gym management software"].competition == "MEDIUM"
    assert by_key["gym management software"].competition_index == 61
    assert by_key["gym management software"].average_cpc_micros == 1_500_000
    assert by_key["gym software pricing"].high_top_of_page_bid_micros == 6_000_000
    assert all(i.source == "keyword_planner_import" for i in ideas)


def test_parse_xlsx_without_extra_dependencies():
    ideas, report = parse_keyword_file(_xlsx_bytes(), "export.xlsx")
    assert report["ok"] is True
    assert report["format"] == "xlsx"
    assert report["ideas"] == 2
    assert {normalize_keyword(i.text) for i in ideas} == {
        "gym management software",
        "gym crm",
    }
    assert next(i for i in ideas if i.text == "gym crm").avg_monthly_searches == 1200


def test_parse_reports_missing_keyword_column():
    ideas, report = parse_keyword_file(b"Foo,Bar\n1,2\n", "weird.csv")
    assert ideas == []
    assert report["ok"] is False


def test_parse_manual_column_mapping():
    data = b"Term;Impressions\nfitness studio software;540\n"
    ideas, report = parse_keyword_file(data, "m.csv", mapping={"keyword": 0, "volume": 1})
    assert report["ok"] is True
    assert len(ideas) == 1
    assert ideas[0].text == "fitness studio software"
    assert ideas[0].avg_monthly_searches == 540


def _fake_upload(data: bytes, filename: str):
    class _Upload:
        def __init__(self):
            self.filename = filename

        async def read(self):
            return data

    return _Upload()


def test_import_start_creates_run_and_analyses_keywords():
    from app.api import research as R
    from app.services.research import research_pipeline
    from tests.helpers import make_member, make_pb, make_project, make_req, make_user

    pb = make_pb()
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])

    resp = asyncio.run(
        R.research_import_start(
            req,
            project["id"],
            file=_fake_upload(CSV_EXPORT, "export.csv"),
            name="Imported gym",
            keywords="gym software",
            site="",
            competitors="",
            country="US",
            language="en",
            locale="",
            network="GOOGLE_SEARCH",
            clustering="deterministic",
            goal="grow organic traffic",
            force="",
        )
    )
    events = resp.headers.get("HX-Trigger", "")
    assert "delayed-redirect" in events

    runs = ResearchRunRepo(pb).list_for_project(project["id"], page=1, per_page=10)
    assert len(runs) == 1
    run = runs[0]
    assert run["researchType"] == "import"
    assert run["config"]["importPayload"]
    assert run["config"]["importName"] == "export.csv"
    assert run["connection"] in ("", None)

    # Run the pipeline: the import branch must persist keywords with no Google Ads.
    from tests.fake_providers import FakeRegistry
    from tests.test_wordpress_publishing import make_ctx

    job = {"id": "j1", "project": project["id"], "payload": {"runId": run["id"]}}
    ctx = make_ctx(pb, FakeRegistry(), job)
    summary = asyncio.run(research_pipeline(ctx, run=run))
    assert "validate" in summary["stages"]
    stage = ResearchRunRepo(pb).get(run["id"])["stageState"]["keyword_collection"]
    assert stage["ideas"] == 3
    metrics = KeywordMetricRepo(pb).list_for_run(run["id"], per_page=50)
    assert len(metrics) == 3
    volumes = sorted(m["avgMonthlySearches"] for m in metrics)
    assert volumes == [320, 1200, 8100]

    # No LLM is configured in FakeRegistry, so the deterministic fallback must
    # still produce reviewable ideas instead of an empty opportunities stage.
    from app.repositories.research import ArticleIdeaRepo

    opp = ResearchRunRepo(pb).get(run["id"])["stageState"]["opportunities"]
    assert opp["created"] > 0, opp
    ideas = ArticleIdeaRepo(pb).list_for_run(run["id"], per_page=10)
    assert ideas
    assert all((i.get("evidence") or {}).get("source") == "deterministic" for i in ideas)
    assert all(i["primaryKeyword"] for i in ideas)


def test_import_preview_returns_detected_columns():
    from app.api import research as R
    from tests.helpers import make_member, make_pb, make_project, make_req, make_user

    pb = make_pb()
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])

    resp = asyncio.run(
        R.research_import_preview(
            req,
            project["id"],
            file=_fake_upload(CSV_EXPORT, "export.csv"),
            mapping_column="",
        )
    )
    body = resp.body.decode()
    assert "Detected" in body
    assert "keyword" in body


def test_import_start_without_file_is_rejected():
    from app.api import research as R
    from tests.helpers import make_member, make_pb, make_project, make_req, make_user

    pb = make_pb()
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])

    resp = asyncio.run(
        R.research_import_start(
            req,
            project["id"],
            file=None,
            name="",
            keywords="",
            site="",
            competitors="",
            country="US",
            language="en",
            locale="",
            network="GOOGLE_SEARCH",
            clustering="auto",
            goal="",
            force="",
        )
    )
    assert "show-toast" in resp.headers.get("HX-Trigger", "")
    assert ResearchRunRepo(pb).list_for_project(project["id"], page=1, per_page=5) == []
