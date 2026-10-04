"""SEO enforcement never injects non-English text into an English pipeline."""

import re

from app.domain.seo_enforce import enforce_article_html, enforce_outline

FA = re.compile(r"[\u0600-\u06FF]")


def _sections():
    return [
        {"heading": "Choosing an App", "content": "<p>Some advice about tracking workouts.</p>"},
        {"heading": "The Shortlist", "content": "<p>More advice.</p>"},
    ]


def test_fallback_intro_contains_keyword_and_is_english():
    out = enforce_article_html(
        title="Best Tracker", slug="best-tracker", sections=_sections(), keyword="gym tracker"
    )
    assert "gym tracker" in out["html"].lower()
    assert not FA.search(out["html"]), "fallback intro must not be Persian"


def test_no_intro_injected_when_keyword_already_in_first_paragraph():
    sections = [
        {"heading": "Choosing", "content": "<p>The best gym tracker logs sets and reps.</p>"}
    ]
    out = enforce_article_html(title="T", slug="t", sections=sections, keyword="gym tracker")
    assert "is one of the key topics" not in out["html"]


def test_empty_section_heading_fallback_is_english():
    outline = enforce_outline({"title": "T", "sections": [{"heading": "", "content": ""}]}, "kw")
    assert outline["sections"][0]["heading"] == "Introduction — kw"
    assert not FA.search(outline["sections"][0]["heading"])
