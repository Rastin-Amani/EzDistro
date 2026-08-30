"""i18n: locale resolution, translation catalogs, direction, locale-aware filters."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.i18n import (
    DEFAULT_LOCALE,
    ENABLED_LOCALES,
    LOCALES,
    _,
    get_locale,
    ngettext,
    set_request_locale,
)
from app.main import app
from app.templates import loc_date, loc_year, status_label, to_rel_time

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_locale():
    """Direct set_request_locale() calls must not leak into other test files
    (contextvars persist within the worker thread between tests)."""
    yield
    set_request_locale(None)


def _use(code: str | None) -> None:
    set_request_locale(code)


# ---------------------------------------------------------------------------
# Registry + resolution
# ---------------------------------------------------------------------------
def test_registry_metadata():
    fa, en = LOCALES["fa"], LOCALES["en"]
    assert fa.is_rtl and fa.default and fa.enabled
    assert not en.is_rtl and en.enabled
    assert "hy" in LOCALES and "hy" not in ENABLED_LOCALES  # readiness ≠ enabled


def test_default_is_fa_and_unknown_collapses_to_default():
    _use(None)
    assert get_locale().code == DEFAULT_LOCALE.code == "fa"
    _use("not-a-locale")
    assert get_locale().code == "fa"
    _use("hy")  # disabled → default
    assert get_locale().code == "fa"


def test_middleware_sets_locale_from_cookie():
    resp = client.get("/login", follow_redirects=False)
    assert 'lang="fa"' in resp.text and 'dir="rtl"' in resp.text
    assert "ورود به سئوز" in resp.text

    resp = client.get("/login", cookies={"locale": "en"})
    assert 'lang="en"' in resp.text and 'dir="ltr"' in resp.text
    assert "Sign in to Seoz" in resp.text

    resp = client.get("/login", cookies={"locale": "zz"})
    assert 'dir="rtl"' in resp.text and "ورود به سئوز" in resp.text


def test_htmx_partial_renders_in_request_locale():
    resp = client.get("/login", headers={"HX-Request": "true"}, cookies={"locale": "en"})
    assert resp.status_code == 200
    assert "Sign in to Seoz" in resp.text


# ---------------------------------------------------------------------------
# Translation behavior
# ---------------------------------------------------------------------------
def test_translation_and_fallback():
    _use("fa")
    assert _("ورود به سئوز") == "ورود به سئوز"  # source language
    _use("en")
    assert _("ورود به سئوز") == "Sign in to Seoz"
    assert _("__msgid_missing_from_catalog__") == "__msgid_missing_from_catalog__"  # fails safe


def test_ngettext_plural():
    _use("en")
    assert ngettext("۱ دقیقه پیش", "%(n)d دقیقه پیش", 1) == "1 minute ago"
    plural = ngettext("۱ دقیقه پیش", "%(n)d دقیقه پیش", 3)
    assert plural % {"n": 3} == "3 minutes ago"


def test_locale_switch_route():
    """Auth gate runs first for unauthenticated requests, so exercise the handler
    directly (with a live PB the route behaves identically post-auth)."""
    from app.main import switch_locale

    resp = switch_locale("en", "/")
    assert resp.status_code == 303
    assert "locale=en" in resp.headers.get("set-cookie", "")

    assert switch_locale("xx", "/").status_code == 404
    assert switch_locale("en", "//evil.com").headers["location"] == "/"

    resp = client.get("/locale/xx", follow_redirects=False)  # auth gate wins unauthenticated
    assert resp.status_code in (303, 404)


def test_switcher_lists_enabled_locales():
    html = client.get("/login").text  # login extends base, no switcher — check via template
    from app.templates import templates

    rendered = templates.env.get_template("components/locale_switcher.html").render(
        _=_,
        locale=type(
            "L", (), {"code": "fa", "enabled": staticmethod(lambda: list(ENABLED_LOCALES.values()))}
        )(),
    )
    assert "/locale/fa" in rendered and "/locale/en" in rendered
    assert "/locale/hy" not in rendered
    assert html  # login page rendered without switcher errors


# ---------------------------------------------------------------------------
# Locale-aware filters
# ---------------------------------------------------------------------------
def test_status_label_locale_aware():
    _use("fa")
    assert status_label("published") == "منتشر شده"
    _use("en")
    assert status_label("published") == "Published"
    assert status_label("__unknown__") == "__unknown__"  # passthrough


def test_dates_jalali_vs_gregorian():
    iso = "2026-08-30 10:00:00"
    _use("en")
    assert "2026" in loc_year(iso) and "2026" in loc_date(iso)
    _use("fa")
    jalali_year = str(loc_year(iso))
    assert "2026" not in jalali_year  # Persian calendar year (~1405)
    assert "2026" not in loc_date(iso)


def test_rel_time_locale_aware():
    import datetime as dt

    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    ten_min_ago = past.strftime("%Y-%m-%d %H:%M:%S")
    _use("fa")
    assert "دقیقه پیش" in to_rel_time(ten_min_ago)
    _use("en")
    assert "minute" in to_rel_time(ten_min_ago)


def test_en_catalog_placeholders_survive_formatting():
    """Regression: an unescaped % in an en translation (e.g. '%(n)s%')
    raises ValueError: incomplete format at render time (fa tests miss it)."""
    import re

    from app.i18n import _, _get_translation

    _use("en")
    spec = re.compile(r"%\(([^)]+)\)[sd]")
    catalog = _get_translation("en")._catalog  # type: ignore[attr-defined]
    for msgid in list(catalog):
        if not isinstance(msgid, str) or "%(" not in msgid:
            continue
        translated = _(msgid)
        values = {k: 1 for k in spec.findall(translated)}
        translated % values  # must not raise
    assert _("%(n)s٪") % {"n": 33} == "33%"
