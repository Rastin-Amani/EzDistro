"""Bulk / CSV topic import — service and route tests.

Covers the parsing layer (delimiter detection, Persian/English header aliases,
quoted CSV), the dedup/validation rules, the import summary counters, and the
two route-level steps (preview → column mapping, confirm → create).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import projects as P
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from app.services.topic_import import (
    build_column_map,
    build_payload,
    detect_columns,
    detect_delimiter,
    detect_header,
    get_preview,
    import_rows,
    normalize_key,
    parse_delimited,
    store_preview,
)
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import call_route_async

SAMPLE_CSV = """Week,ID,Title,Keyword,Related Pillar,Cluster,Type,Written,Published,URL
1,1,Why does the heater blow warm air uphill?,Heater fault,Maintenance,HVAC system,troubleshooting guide,1,1,https://cheraq-check.ir/?p=3397
1,2,Clunking steering noise when turning,Clunking steering noise,Technical diagnosis,Front end,Q&A,0,0,
2,3,"Genuine vs counterfeit pads for a 206 + how to tell",Spotting genuine 206 pads,Spare parts,Brakes,buying guide,0,0,"""

PIPE_PASTE = """Topic One | Keyword One | Pillar A | Cluster 1 | guide | 5
Topic Two | Keyword Two"""

TAB_PASTE = "Title\tKeyword\tPillar\tCluster\tType\tPriority\nA\tB\tC\tD\tguide\t3"


def make_pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def make_user(uid: str = "u1", role: str = "member") -> dict:
    return {"id": uid, "role": role, "email": f"{uid}@x.com"}


def make_req(pb: FakePocketBase, user: dict, project_id: str) -> SimpleNamespace:
    def url_for(name: str, **path_params):
        return "/" + name

    return SimpleNamespace(
        state=SimpleNamespace(pb=pb, user=user, req_id="r1"),
        headers={"HX-Request": "true"},
        url=SimpleNamespace(path=f"/projects/{project_id}"),
        query_params={},
        url_for=url_for,
    )


def toast_message(resp) -> str:
    """Extract the toast text from a mutation response's HX-Trigger header."""
    events = json.loads(resp.headers.get("HX-Trigger", "{}"))
    return events.get("show-toast", {}).get("message", "")


@pytest.fixture()
def setup():
    """User u1 owns project A."""
    pb = make_pb()
    proj = ProjectRepo(pb).create(name="A", slug="proj-a")
    MemberRepo(pb).add(project=proj["id"], user="u1", role="owner")
    return {"pb": pb, "proj": proj}


# ---------------------------------------------------------------------------
# Parsing layer
# ---------------------------------------------------------------------------
def test_detect_delimiter_auto():
    assert detect_delimiter("a,b,c\nd,e,f") == ","
    assert detect_delimiter("a\tb\tc\nd\te\tf") == "\t"
    assert detect_delimiter("a|b|c\nd|e|f") == "|"
    assert detect_delimiter("a;b;c\nd;e;f") == ";"
    # quoted comma must not confuse the detector
    assert detect_delimiter('"a,b",c\nd,e') == ","


def test_parse_quoted_csv_keeps_commas():
    delim, rows = parse_delimited(SAMPLE_CSV)
    assert delim == ","
    assert len(rows) == 4
    assert "Genuine vs counterfeit pads for a 206 + how to tell" in rows[3]


def test_header_detection():
    assert detect_header(parse_delimited(SAMPLE_CSV)[1]) is True
    header = "Week,Title,Keyword,Pillar,Cluster,Type\n1,A,B,C,D,guide"
    delim, rows = parse_delimited(header)
    assert delim == ","
    assert detect_header(rows) is True
    assert detect_header(parse_delimited(PIPE_PASTE)[1]) is False


def test_auto_column_map_sample_csv():
    _delim, rows = parse_delimited(SAMPLE_CSV)
    columns = detect_columns(rows, has_header=True)
    mapped = build_column_map(columns, has_header=True)
    assert mapped == {
        "week": 0,
        "title": 2,
        "keyword": 3,
        "pillar": 4,
        "cluster": 5,
        "type": 6,
        "url": 9,
    }


def test_no_header_maps_first_column_to_title():
    _delim, rows = parse_delimited(PIPE_PASTE)
    columns = detect_columns(rows, has_header=False)
    assert build_column_map(columns, has_header=False) == {"title": 0}


def test_numeric_columns_map_and_parse():
    _delim, rows = parse_delimited(TAB_PASTE)
    assert detect_header(rows) is True
    mapped = build_column_map(detect_columns(rows, True), True)
    assert mapped["priority"] == 5
    payload, reason = build_payload(rows[1], mapped, set(), set(), set(), set())
    assert payload["priority"] == 3
    assert payload["type"] == "guide"


# ---------------------------------------------------------------------------
# Row → payload rules
# ---------------------------------------------------------------------------
def _cmap(**fields) -> dict:
    return fields


def test_build_payload_ok():
    row = [
        "Sample title",
        "Sample key",
        "Pillar",
        "Cluster",
        "guide",
        "7",
        "12",
        "https://x.ir/1",
    ]
    payload, reason = build_payload(
        row,
        {
            "title": 0,
            "keyword": 1,
            "pillar": 2,
            "cluster": 3,
            "type": 4,
            "priority": 5,
            "week": 6,
            "url": 7,
        },
        set(),
        set(),
        set(),
        set(),
    )
    assert reason is None
    assert payload["title"] == "Sample title"
    assert payload["type"] == "guide"
    assert payload["priority"] == 7
    assert payload["week"] == 12
    assert payload["url"] == "https://x.ir/1"


def test_build_payload_empty_title():
    payload, reason = build_payload(["  "], {"title": 0}, set(), set(), set(), set())
    assert payload is None
    assert reason == "Title is empty"


def test_build_payload_duplicate_title_and_keyword():
    row = [
        "duplicate",
        "Duplicate key",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    cmap = {"title": 0, "keyword": 1}
    _p, r1 = build_payload(row, cmap, {normalize_key("duplicate")}, set(), set(), set())
    assert r1 == "duplicate"
    _p, r2 = build_payload(
        row,
        cmap,
        set(),
        {normalize_key("Duplicate key")},
        set(),
        set(),
    )
    assert r2 == "duplicate"
    # duplicate within the same batch
    _p, r3 = build_payload(row, cmap, set(), set(), {normalize_key("duplicate")}, set())
    assert r3 == "duplicate"


def test_build_payload_coerces_freeform_type():
    row = [
        "Heading",
        "Key",
        "",
        "",
        "Q&A",
        "",
        "",
        "",
    ]
    payload, reason = build_payload(
        row, {"title": 0, "keyword": 1, "type": 4}, set(), set(), set(), set()
    )
    assert reason == "coerced"
    assert payload["type"] == "article"


# ---------------------------------------------------------------------------
# import_rows
# ---------------------------------------------------------------------------
def test_import_rows_full_cycle(setup):
    pb, proj = setup["pb"], setup["proj"]
    _delim, rows = parse_delimited(SAMPLE_CSV)
    mapped = build_column_map(detect_columns(rows, True), True)

    first = import_rows(pb, proj["id"], rows, mapped, has_header=True)
    assert first["created"] == 3
    assert first["coerced"] == 3  # troubleshooting guide + Q&A + buying guide → article
    assert first["errors"] == []

    # rerun → everything is a duplicate
    second = import_rows(pb, proj["id"], rows, mapped, has_header=True)
    assert second["created"] == 0
    assert second["skipped"] == 3

    stored = TopicRepo(pb).list_for_project(proj["id"], per_page=50)
    assert len(stored) == 3
    with_url = next(r for r in stored if r.get("url"))
    assert with_url["week"] == 1
    assert with_url["url"] == "https://cheraq-check.ir/?p=3397"


def test_import_rows_collects_errors(setup):
    pb, proj = setup["pb"], setup["proj"]
    rows = [
        ["T", "K", "Type"],
        [
            "A",
            "K1",
            "troubleshooting guide",
        ],  # coerced → created
        ["A", "K2", ""],  # duplicate title → skipped
        ["", "K3", ""],  # empty title → error
    ]
    summary = import_rows(
        pb, proj["id"], rows, {"title": 0, "keyword": 1, "type": 2}, has_header=True
    )
    assert summary["created"] == 1
    assert summary["skipped"] == 1
    assert summary["coerced"] == 1
    assert summary["error_count"] == 1
    assert summary["errors"][0]["row"] == 4
    assert "Title is empty" in summary["errors"][0]["reason"]


def test_preview_cache_roundtrip():
    token = store_preview(
        rows=[["a"]],
        delimiter=",",
        has_header=False,
        columns=[{"index": 0, "name": "a", "samples": []}],
        filename="x.csv",
    )
    entry = get_preview(token)
    assert entry is not None
    assert entry["rows"] == [["a"]]
    assert get_preview("bogus") is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def test_import_preview_renders_mapping_form(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])
    resp = await call_route_async(
        P.topic_import_preview, req, setup["proj"]["id"], csv_text=PIPE_PASTE
    )
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "col_title" in body
    assert "Topic One" in body  # preview sample row
    assert "No header" in body
    assert 'name="token"' in body
    assert 'aria-current="step"' in body and "Map columns" in body


async def test_import_preview_empty_input(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])
    resp = await call_route_async(P.topic_import_preview, req, setup["proj"]["id"], csv_text="")
    assert resp.status_code == 200
    assert "No text or file provided" in toast_message(resp)


async def test_import_confirm_creates_topics(setup):
    pb, proj = setup["pb"], setup["proj"]
    _delim, rows = parse_delimited(SAMPLE_CSV)
    mapped = build_column_map(detect_columns(rows, True), True)
    token = store_preview(
        rows=rows,
        delimiter=",",
        has_header=True,
        columns=detect_columns(rows, True),
        filename="s.csv",
    )
    req = make_req(pb, make_user(), proj["id"])

    async def fake_form():
        form = {"token": token}
        for field, index in mapped.items():
            form[f"col_{field}"] = str(index)
        return form

    req.form = fake_form
    resp = await call_route_async(P.topic_import, req, proj["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "Added" in body
    assert 'aria-current="step"' in body and "Review results" in body
    assert TopicRepo(pb).list_for_project(proj["id"], per_page=50)  # non-empty


async def test_import_confirm_requires_title_column(setup):
    pb, proj = setup["pb"], setup["proj"]
    token = store_preview(
        rows=[["A", "B"]],
        delimiter=",",
        has_header=False,
        columns=[{"index": 0, "name": "Pillar 1", "samples": []}],
        filename="s.csv",
    )
    req = make_req(pb, make_user(), proj["id"])

    async def fake_form():
        return {"token": token, "col_keyword": "1"}

    req.form = fake_form
    resp = await call_route_async(P.topic_import, req, proj["id"])
    assert resp.status_code == 200
    assert "Select the title column" in toast_message(resp)


async def test_import_confirm_stale_token(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])

    async def fake_form():
        return {"token": "expired", "col_title": "0"}

    req.form = fake_form
    resp = await call_route_async(P.topic_import, req, setup["proj"]["id"])
    assert resp.status_code == 200
    assert "expired" in toast_message(resp)


def test_import_form_back_button_partial(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])
    resp = P.topic_import_form(req, setup["proj"]["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "import/preview" in body
    assert "csv_text" in body
    assert 'dir="auto"' in body
    assert 'aria-current="step"' in body and "Preview and map columns" in body
