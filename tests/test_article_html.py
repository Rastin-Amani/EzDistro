import pytest

from app.domain.article_html import build_article_html, normalize_outline, slugify
from app.domain.sanitize import sanitize_html

GOOD_OUTLINE = {
    "title": "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u06a9\u0627\u0645\u0644 \u0633\u0626\u0648",
    "slug": "rahnama-seo",
    "sections": [
        {
            "heading": "\u0645\u0642\u062f\u0645\u0647",
            "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0645\u0642\u062f\u0645\u0647",
            "internal_links": [
                {
                    "title": "\u0645\u0642\u0627\u0644\u0647 \u0645\u0631\u062a\u0628\u0637",
                    "url": "https://example.com/related",
                    "anchor_text": "\u0645\u0642\u0627\u0644\u0647 \u0645\u0631\u062a\u0628\u0637",
                }
            ],
        },
        {
            "heading": "\u062a\u06a9\u0646\u06cc\u06a9\u200c\u0647\u0627",
            "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u062a\u06a9\u0646\u06cc\u06a9\u200c\u0647\u0627",
        },
    ],
}


def test_normalize_outline_valid():
    result = normalize_outline(GOOD_OUTLINE)
    assert (
        result["title"]
        == "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u06a9\u0627\u0645\u0644 \u0633\u0626\u0648"
    )
    assert result["slug"] == "rahnama-seo"
    assert len(result["sections"]) == 2
    assert result["sections"][0]["internal_links"][0]["url"] == "https://example.com/related"
    assert (
        result["sections"][0]["content_brief"]
        == "\u062e\u0644\u0627\u0635\u0647 \u0645\u0642\u062f\u0645\u0647"
    )


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
    assert (
        slugify(
            "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u06a9\u0627\u0645\u0644 \u0633\u0626\u0648"
        )
        == "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc-\u06a9\u0627\u0645\u0644-\u0633\u0626\u0648"
    )
    assert slugify("Hello World!") == "hello-world"
    assert slugify("") == "post"


def test_build_article_html_structure():
    html = build_article_html(
        title="\u062a\u06cc\u062a\u0631 \u0627\u0635\u0644\u06cc",
        slug="titr-asli",
        sections=[
            {
                "heading": "\u0628\u062e\u0634 \u06f1",
                "content": "<p>\u0645\u062d\u062a\u0648\u0627</p><script>alert(1)</script>",
            }
        ],
        internal_links=[
            {
                "title": "\u0644\u06cc\u0646\u06a9",
                "url": "https://x.com",
                "anchor_text": "\u0644\u06cc\u0646\u06a9",
            }
        ],
    )
    assert "<h1>\u062a\u06cc\u062a\u0631 \u0627\u0635\u0644\u06cc</h1>" in html
    assert "<h2>\u0628\u062e\u0634 \u06f1</h2>" in html
    assert "<script>" not in html  # sanitized inside build
    assert "\u0645\u0637\u0627\u0644\u0628 \u0645\u0631\u062a\u0628\u0637" in html
    assert 'href="https://x.com"' in html


def test_build_article_html_dedupes_headings_and_keeps_order():
    html = build_article_html(
        title="\u062a",
        sections=[
            {"heading": "\u062a\u06a9\u0631\u0627\u0631", "content": "<p>\u0627\u0648\u0644</p>"},
            {"heading": "\u062f\u06cc\u06af\u0631", "content": "<p>\u062f\u0648\u0645</p>"},
            {"heading": "\u062a\u06a9\u0631\u0627\u0631", "content": "<p>\u0633\u0648\u0645</p>"},
        ],
    )
    assert (
        html.index("<h2>\u062a\u06a9\u0631\u0627\u0631</h2>")
        < html.index("<h2>\u062f\u06cc\u06af\u0631</h2>")
        < html.index("<h2>\u062a\u06a9\u0631\u0627\u0631 (2)</h2>")
    )
    assert html.count("<h2>\u062a\u06a9\u0631\u0627\u0631") == 2
    assert "<br>" not in html  # no arbitrary separators
    assert "<hr>" not in html


def test_build_article_html_removes_markdown_fences():
    html = build_article_html(
        title="\u062a",
        sections=[
            {
                "heading": "\u0628",
                "content": "<p>\u0645\u062a\u0646</p>" + "```" + "\n\u06a9\u062f\n" + "```",
            }
        ],
    )
    assert "```" not in html
    assert "\u06a9\u062f" in html  # fence markers removed; inner content kept as text


def test_build_article_html_escapes_title():
    html = build_article_html(
        title='<script>alert("x")</script>',
        sections=[{"heading": "\u0628", "content": "<p>c</p>"}],
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
        '<h2>\u062a\u06cc\u062a\u0631</h2><p><strong>bold</strong> <a href="https://ok.com">link</a></p><ul><li>x</li></ul>'
    )
    assert "<h2>\u062a\u06cc\u062a\u0631</h2>" in clean
    assert "<strong>bold</strong>" in clean
    assert 'href="https://ok.com"' in clean
