"""Deterministic SEO enforcement — guarantees the article score floor.

Prompts steer the LLM; this module guarantees. Pure string transforms applied at
outline-generation and assembly time so an article can never ship without the
keyword in the title (= H1), the URL slug, the first paragraph, and at least one
H2 heading.

Score floor (see seo_score.py): keyword-in-title (25) + keyword-in-first-paragraph
(15) + keyword-in-a-heading (20) + sane density (10) + structure (≤50). With the
first three guaranteed and a non-empty meta description, the worst case is
25+15+20+0+30 = 90 — the acceptable floor.
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.article_html import build_article_html, slugify


def enforce_title(title: str, keyword: str) -> str:
    """Return a title guaranteed to contain the keyword (prefix when missing)."""
    kw = (keyword or "").strip()
    t = (title or "").strip()
    if not kw:
        return t
    if not t:
        return kw
    if kw.lower() in t.lower():
        return t
    return f"{kw} | {t}"


def enforce_outline(outline: dict[str, Any], keyword: str) -> dict[str, Any]:
    """Guarantee keyword placement in the outline snapshot (title + an H2)."""
    kw = (keyword or "").strip()
    if not kw or not outline:
        return outline
    out = dict(outline)
    out["title"] = enforce_title(out.get("title") or "", kw)
    sections = list(out.get("sections") or [])
    if sections and not _any_heading_has_kw(sections, kw):
        first = dict(sections[0])
        first["heading"] = _heading_with_kw(first.get("heading") or "Introduction", kw)
        sections[0] = first
        out["sections"] = sections
    return out


def enforce_article_html(
    *,
    title: str,
    slug: str,
    sections: list[dict[str, Any]],
    internal_links: list[dict[str, Any]] | None = None,
    keyword: str = "",
    sanitize: bool = True,
) -> dict[str, Any]:
    """Assemble the final HTML with keyword placement guaranteed.

    Returns {"title", "slug", "html"} — the caller persists title/slug/html.

    ``sanitize`` defaults to True (safe for untrusted section content). Callers
    passing already-sanitized stored section HTML (the assembler receives
    section content sanitized when each section completed) set it False to skip
    re-sanitizing trusted data.
    """
    kw = (keyword or "").strip()
    fixed_title = enforce_title(title, kw)
    fixed_slug = slug or slugify(fixed_title)

    sections = list(sections)
    if kw and sections and not _any_heading_has_kw(sections, kw):
        first = dict(sections[0])
        first["heading"] = _heading_with_kw(first.get("heading") or "Introduction", kw)
        sections[0] = first

    def _build(intro: str = "") -> str:
        return build_article_html(
            title=fixed_title,
            slug=fixed_slug,
            sections=sections,
            internal_links=internal_links,
            intro_paragraph=intro,
            sanitize=sanitize,
        )

    html = _build()
    if kw and not _first_paragraph_contains(html, kw):
        html = _build(_intro_paragraph(kw))
    return {"title": fixed_title, "slug": fixed_slug, "html": html}


def _any_heading_has_kw(sections: list[dict[str, Any]], keyword: str) -> bool:
    kw = keyword.lower()
    return any(kw in (s.get("heading") or "").lower() for s in sections)


def _heading_with_kw(heading: str, keyword: str) -> str:
    return f"{heading} — {keyword}"


def _first_paragraph_contains(html: str, keyword: str) -> bool:
    kw = keyword.lower()
    match = re.search(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return False
    return kw in re.sub(r"<[^>]+>", "", match.group(1)).lower()


def _intro_paragraph(keyword: str) -> str:
    """Fallback lede used when the first paragraph lacks the keyword.
    English only — the content pipeline is English-only."""
    kw = keyword.strip()
    return (
        f"{kw} is one of the key topics this article covers. "
        f"Below we look at {kw} and the details that matter most when choosing."
    )
