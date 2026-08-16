import pytest

from app.domain.article_html import build_article_html, normalize_outline, slugify
from app.domain.sanitize import sanitize_html

GOOD_OUTLINE = {
    "title": "راهنمای کامل سئو",
    "slug": "rahnama-seo",
    "sections": [
        {
            "heading": "مقدمه",
            "content_brief": "خلاصه مقدمه",
            "internal_links": [
                {
                    "title": "مقاله مرتبط",
                    "url": "https://example.com/related",
                    "anchor_text": "مقاله مرتبط",
                }
            ],
        },
        {"heading": "تکنیک‌ها", "content_brief": "خلاصه تکنیک‌ها"},
    ],
}


def test_normalize_outline_valid():
    result = normalize_outline(GOOD_OUTLINE)
    assert result["title"] == "راهنمای کامل سئو"
    assert result["slug"] == "rahnama-seo"
    assert len(result["sections"]) == 2
    assert result["sections"][0]["internal_links"][0]["url"] == "https://example.com/related"
    assert result["sections"][0]["content_brief"] == "خلاصه مقدمه"


def test_normalize_outline_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_outline("not a dict")
    with pytest.raises(ValueError):
        normalize_outline({})
    with pytest.raises(ValueError):
        normalize_outline({"title": "", "sections": []})
    with pytest.raises(ValueError):
        normalize_outline({"title": "x", "sections": [{"heading": "only one"}]})


def test_slugify_persian_and_ascii():
    assert slugify("راهنمای کامل سئو") == "راهنمای-کامل-سئو"
    assert slugify("Hello World!") == "hello-world"
    assert slugify("") == "post"


def test_build_article_html_structure():
    html = build_article_html(
        title="تیتر اصلی",
        slug="titr-asli",
        sections=[{"heading": "بخش ۱", "content": "<p>محتوا</p><script>alert(1)</script>"}],
        internal_links=[{"title": "لینک", "url": "https://x.com", "anchor_text": "لینک"}],
    )
    assert "<h1>تیتر اصلی</h1>" in html
    assert "<h2>بخش ۱</h2>" in html
    assert "<script>" not in html  # sanitized inside build
    assert "مطالب مرتبط" in html
    assert 'href="https://x.com"' in html


def test_build_article_html_dedupes_headings_and_keeps_order():
    html = build_article_html(
        title="ت",
        sections=[
            {"heading": "تکرار", "content": "<p>اول</p>"},
            {"heading": "دیگر", "content": "<p>دوم</p>"},
            {"heading": "تکرار", "content": "<p>سوم</p>"},
        ],
    )
    assert (
        html.index("<h2>تکرار</h2>")
        < html.index("<h2>دیگر</h2>")
        < html.index("<h2>تکرار (2)</h2>")
    )
    assert html.count("<h2>تکرار") == 2
    assert "<br>" not in html  # no arbitrary separators
    assert "<hr>" not in html


def test_build_article_html_removes_markdown_fences():
    html = build_article_html(
        title="ت",
        sections=[{"heading": "ب", "content": "<p>متن</p>" + "```" + "\nکد\n" + "```"}],
    )
    assert "```" not in html
    assert "کد" in html  # fence markers removed; inner content kept as text


def test_build_article_html_escapes_title():
    html = build_article_html(
        title='<script>alert("x")</script>',
        sections=[{"heading": "ب", "content": "<p>c</p>"}],
    )
    assert "<script>" not in html


def test_sanitize_removes_dangerous_html():
    raw = '<p onclick="steal()">ok</p><iframe src="x"></iframe><a href="javascript:x">y</a><img src=x onerror=alert(1)>'
    clean = sanitize_html(raw)
    assert "onclick" not in clean
    assert "iframe" not in clean
    assert "javascript:" not in clean
    assert "onerror" not in clean
    assert "ok" in clean


def test_sanitize_keeps_allowlist():
    clean = sanitize_html(
        '<h2>تیتر</h2><p><strong>bold</strong> <a href="https://ok.com">link</a></p><ul><li>x</li></ul>'
    )
    assert "<h2>تیتر</h2>" in clean
    assert "<strong>bold</strong>" in clean
    assert 'href="https://ok.com"' in clean
