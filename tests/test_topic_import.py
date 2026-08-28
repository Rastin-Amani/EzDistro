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

SAMPLE_CSV = """Week,ID,Title,Keyword,Related Pillar,Cluster,Type,Written,Published,URL
1,1,چرا کولر شاهین در سربالایی باد گرم می‌زند؟,مشکل کولر شاهین,تعمیر و نگهداری,سیستم تهویه,راهنمای عیب‌یابی,1,1,https://cheraq-check.ir/?p=3397
1,2,صدای تق‌تق فرمان پراید هنگام پیچیدن,صدای تق تق فرمان پراید,عیب‌یابی فنی,جلوبندی,پرسش و پاسخ,0,0,
2,3,"تفاوت لنت ترمز تکستار اصلی و تقلبی برای ۲۰۶ تیپ ۵ + عکس تشخیص",تشخیص لنت تکستار اصلی ۲۰۶,قطعات یدکی,سیستم ترمز,راهنمای خرید,0,0,"""

PIPE_PASTE = """موضوع اول | کلمه اول | ستون الف | خوشه ۱ | راهنما | 5
موضوع دوم | کلمه دوم"""

TAB_PASTE = "عنوان\tکلمه\tستون\tخوشه\tنوع\tاولویت\nالف\tب\tج\tد\tراهنما\t۳"


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
    assert "تفاوت لنت ترمز تکستار اصلی و تقلبی برای ۲۰۶ تیپ ۵ + عکس تشخیص" in rows[3]


def test_header_detection_en_and_fa():
    assert detect_header(parse_delimited(SAMPLE_CSV)[1]) is True
    fa = "هفته،عنوان،کلمه کلیدی،ستون،خوشه،نوع\n۱،الف،ب،ج،د،راهنما"
    delim, rows = parse_delimited(fa)
    assert delim == "،"
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
    row = ["عنوان نمونه", "کلید نمونه", "ستون", "خوشه", "راهنما", "7", "12", "https://x.ir/1"]
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
    assert payload["title"] == "عنوان نمونه"
    assert payload["type"] == "guide"
    assert payload["priority"] == 7
    assert payload["week"] == 12
    assert payload["url"] == "https://x.ir/1"


def test_build_payload_empty_title():
    payload, reason = build_payload(["  "], {"title": 0}, set(), set(), set(), set())
    assert payload is None
    assert reason == "عنوان خالی است"


def test_build_payload_duplicate_title_and_keyword():
    row = ["تکراری", "کلید تکراری", "", "", "", "", "", ""]
    cmap = {"title": 0, "keyword": 1}
    _p, r1 = build_payload(row, cmap, {normalize_key("تکراری")}, set(), set(), set())
    assert r1 == "duplicate"
    _p, r2 = build_payload(row, cmap, set(), {normalize_key("کلید تکراری")}, set(), set())
    assert r2 == "duplicate"
    # duplicate within the same batch
    _p, r3 = build_payload(row, cmap, set(), set(), {normalize_key("تکراری")}, set())
    assert r3 == "duplicate"


def test_build_payload_coerces_freeform_type():
    row = ["تیتر", "کلید", "", "", "پرسش و پاسخ", "", "", ""]
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
    assert first["coerced"] == 3  # راهنمای عیب‌یابی + پرسش و پاسخ + راهنمای خرید → article
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
        ["T", "K", "نوع"],
        ["الف", "ک1", "راهنمای عیب‌یابی"],  # coerced → created
        ["الف", "ک2", ""],  # duplicate title → skipped
        ["", "ک3", ""],  # empty title → error
    ]
    summary = import_rows(
        pb, proj["id"], rows, {"title": 0, "keyword": 1, "type": 2}, has_header=True
    )
    assert summary["created"] == 1
    assert summary["skipped"] == 1
    assert summary["coerced"] == 1
    assert summary["error_count"] == 1
    assert summary["errors"][0]["row"] == 4
    assert "عنوان خالی است" in summary["errors"][0]["reason"]


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
    resp = await P.topic_import_preview(req, setup["proj"]["id"], csv_text=PIPE_PASTE)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "col_title" in body
    assert "موضوع اول" in body  # preview sample row
    assert "بدون هدر" in body
    assert 'name="token"' in body


async def test_import_preview_empty_input(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])
    resp = await P.topic_import_preview(req, setup["proj"]["id"], csv_text="")
    assert resp.status_code == 200
    assert "متن یا فایلی وارد نشده است" in toast_message(resp)


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
    resp = await P.topic_import(req, proj["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "افزوده شد" in body
    assert TopicRepo(pb).list_for_project(proj["id"], per_page=50)  # non-empty


async def test_import_confirm_requires_title_column(setup):
    pb, proj = setup["pb"], setup["proj"]
    token = store_preview(
        rows=[["الف", "ب"]],
        delimiter=",",
        has_header=False,
        columns=[{"index": 0, "name": "ستون 1", "samples": []}],
        filename="s.csv",
    )
    req = make_req(pb, make_user(), proj["id"])

    async def fake_form():
        return {"token": token, "col_keyword": "1"}

    req.form = fake_form
    resp = await P.topic_import(req, proj["id"])
    assert resp.status_code == 200
    assert "ستون عنوان را انتخاب کنید" in toast_message(resp)


async def test_import_confirm_stale_token(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])

    async def fake_form():
        return {"token": "expired", "col_title": "0"}

    req.form = fake_form
    resp = await P.topic_import(req, setup["proj"]["id"])
    assert resp.status_code == 200
    assert "منقضی شده" in toast_message(resp)


def test_import_form_back_button_partial(setup):
    req = make_req(setup["pb"], make_user(), setup["proj"]["id"])
    resp = P.topic_import_form(req, setup["proj"]["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "import/preview" in body
    assert "csv_text" in body
