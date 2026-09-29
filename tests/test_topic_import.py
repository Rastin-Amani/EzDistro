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
1,1,\u0686\u0631\u0627 \u06a9\u0648\u0644\u0631 \u0634\u0627\u0647\u06cc\u0646 \u062f\u0631 \u0633\u0631\u0628\u0627\u0644\u0627\u06cc\u06cc \u0628\u0627\u062f \u06af\u0631\u0645 \u0645\u06cc\u200c\u0632\u0646\u062f\u061f,\u0645\u0634\u06a9\u0644 \u06a9\u0648\u0644\u0631 \u0634\u0627\u0647\u06cc\u0646,\u062a\u0639\u0645\u06cc\u0631 \u0648 \u0646\u06af\u0647\u062f\u0627\u0631\u06cc,\u0633\u06cc\u0633\u062a\u0645 \u062a\u0647\u0648\u06cc\u0647,\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0639\u06cc\u0628\u200c\u06cc\u0627\u0628\u06cc,1,1,https://cheraq-check.ir/?p=3397
1,2,\u0635\u062f\u0627\u06cc \u062a\u0642\u200c\u062a\u0642 \u0641\u0631\u0645\u0627\u0646 \u067e\u0631\u0627\u06cc\u062f \u0647\u0646\u06af\u0627\u0645 \u067e\u06cc\u0686\u06cc\u062f\u0646,\u0635\u062f\u0627\u06cc \u062a\u0642 \u062a\u0642 \u0641\u0631\u0645\u0627\u0646 \u067e\u0631\u0627\u06cc\u062f,\u0639\u06cc\u0628\u200c\u06cc\u0627\u0628\u06cc \u0641\u0646\u06cc,\u062c\u0644\u0648\u0628\u0646\u062f\u06cc,\u067e\u0631\u0633\u0634 \u0648 \u067e\u0627\u0633\u062e,0,0,
2,3,"\u062a\u0641\u0627\u0648\u062a \u0644\u0646\u062a \u062a\u0631\u0645\u0632 \u062a\u06a9\u0633\u062a\u0627\u0631 \u0627\u0635\u0644\u06cc \u0648 \u062a\u0642\u0644\u0628\u06cc \u0628\u0631\u0627\u06cc \u06f2\u06f0\u06f6 \u062a\u06cc\u067e \u06f5 + \u0639\u06a9\u0633 \u062a\u0634\u062e\u06cc\u0635",\u062a\u0634\u062e\u06cc\u0635 \u0644\u0646\u062a \u062a\u06a9\u0633\u062a\u0627\u0631 \u0627\u0635\u0644\u06cc \u06f2\u06f0\u06f6,\u0642\u0637\u0639\u0627\u062a \u06cc\u062f\u06a9\u06cc,\u0633\u06cc\u0633\u062a\u0645 \u062a\u0631\u0645\u0632,\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u062e\u0631\u06cc\u062f,0,0,"""

PIPE_PASTE = """\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644 | \u06a9\u0644\u0645\u0647 \u0627\u0648\u0644 | \u0633\u062a\u0648\u0646 \u0627\u0644\u0641 | \u062e\u0648\u0634\u0647 \u06f1 | \u0631\u0627\u0647\u0646\u0645\u0627 | 5
\u0645\u0648\u0636\u0648\u0639 \u062f\u0648\u0645 | \u06a9\u0644\u0645\u0647 \u062f\u0648\u0645"""

TAB_PASTE = "\u0639\u0646\u0648\u0627\u0646\t\u06a9\u0644\u0645\u0647\t\u0633\u062a\u0648\u0646\t\u062e\u0648\u0634\u0647\t\u0646\u0648\u0639\t\u0627\u0648\u0644\u0648\u06cc\u062a\n\u0627\u0644\u0641\t\u0628\t\u062c\t\u062f\t\u0631\u0627\u0647\u0646\u0645\u0627\t\u06f3"


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
    assert (
        "\u062a\u0641\u0627\u0648\u062a \u0644\u0646\u062a \u062a\u0631\u0645\u0632 \u062a\u06a9\u0633\u062a\u0627\u0631 \u0627\u0635\u0644\u06cc \u0648 \u062a\u0642\u0644\u0628\u06cc \u0628\u0631\u0627\u06cc \u06f2\u06f0\u06f6 \u062a\u06cc\u067e \u06f5 + \u0639\u06a9\u0633 \u062a\u0634\u062e\u06cc\u0635"
        in rows[3]
    )


def test_header_detection_en_and_fa():
    assert detect_header(parse_delimited(SAMPLE_CSV)[1]) is True
    fa = "\u0647\u0641\u062a\u0647\u060c\u0639\u0646\u0648\u0627\u0646\u060c\u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc\u060c\u0633\u062a\u0648\u0646\u060c\u062e\u0648\u0634\u0647\u060c\u0646\u0648\u0639\n\u06f1\u060c\u0627\u0644\u0641\u060c\u0628\u060c\u062c\u060c\u062f\u060c\u0631\u0627\u0647\u0646\u0645\u0627"
    delim, rows = parse_delimited(fa)
    assert delim == "\u060c"
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


def test_persian_digits_in_numbers():
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
        "\u0639\u0646\u0648\u0627\u0646 \u0646\u0645\u0648\u0646\u0647",
        "\u06a9\u0644\u06cc\u062f \u0646\u0645\u0648\u0646\u0647",
        "\u0633\u062a\u0648\u0646",
        "\u062e\u0648\u0634\u0647",
        "\u0631\u0627\u0647\u0646\u0645\u0627",
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
    assert payload["title"] == "\u0639\u0646\u0648\u0627\u0646 \u0646\u0645\u0648\u0646\u0647"
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
        "\u062a\u06a9\u0631\u0627\u0631\u06cc",
        "\u06a9\u0644\u06cc\u062f \u062a\u06a9\u0631\u0627\u0631\u06cc",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    cmap = {"title": 0, "keyword": 1}
    _p, r1 = build_payload(
        row, cmap, {normalize_key("\u062a\u06a9\u0631\u0627\u0631\u06cc")}, set(), set(), set()
    )
    assert r1 == "duplicate"
    _p, r2 = build_payload(
        row,
        cmap,
        set(),
        {normalize_key("\u06a9\u0644\u06cc\u062f \u062a\u06a9\u0631\u0627\u0631\u06cc")},
        set(),
        set(),
    )
    assert r2 == "duplicate"
    # duplicate within the same batch
    _p, r3 = build_payload(
        row, cmap, set(), set(), {normalize_key("\u062a\u06a9\u0631\u0627\u0631\u06cc")}, set()
    )
    assert r3 == "duplicate"


def test_build_payload_coerces_freeform_type():
    row = [
        "\u062a\u06cc\u062a\u0631",
        "\u06a9\u0644\u06cc\u062f",
        "",
        "",
        "\u067e\u0631\u0633\u0634 \u0648 \u067e\u0627\u0633\u062e",
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
    assert (
        first["coerced"] == 3
    )  # \u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0639\u06cc\u0628\u200c\u06cc\u0627\u0628\u06cc + \u067e\u0631\u0633\u0634 \u0648 \u067e\u0627\u0633\u062e + \u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u062e\u0631\u06cc\u062f → article
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
        ["T", "K", "\u0646\u0648\u0639"],
        [
            "\u0627\u0644\u0641",
            "\u06a91",
            "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0639\u06cc\u0628\u200c\u06cc\u0627\u0628\u06cc",
        ],  # coerced → created
        ["\u0627\u0644\u0641", "\u06a92", ""],  # duplicate title → skipped
        ["", "\u06a93", ""],  # empty title → error
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
    assert "\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644" in body  # preview sample row
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
        rows=[["\u0627\u0644\u0641", "\u0628"]],
        delimiter=",",
        has_header=False,
        columns=[{"index": 0, "name": "\u0633\u062a\u0648\u0646 1", "samples": []}],
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
