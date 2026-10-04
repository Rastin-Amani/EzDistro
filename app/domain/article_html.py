"""Article assembly — deterministic, clean semantic HTML.

The assembler:
- preserves section ordering
- generates H2 headings (deduplicated — no repeated heading text)
- sanitizes invalid HTML per section
- removes markdown fences
- normalizes whitespace
- preserves intended internal links (from the outline snapshot)
- NEVER adds arbitrary <br>/<hr> separators
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.domain.chunker import word_count_from_html
from app.domain.sanitize import sanitize_html

_FENCE_RE = re.compile(r"```|~~~", re.MULTILINE)
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def slugify(text: str, fallback: str = "post") -> str:
    """Unicode-aware slug: keep letters/digits, others → '-', lowercase."""
    if not text:
        return fallback
    text = unicodedata.normalize("NFKC", text.strip())
    text = text.replace("\u200c", "-")
    text = re.sub(r"[^\w-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or fallback).lower()[:120]


def normalize_article_html(raw: str) -> str:
    """Clean model output: strip fences, wrappers, collapse whitespace."""
    if not raw:
        return ""
    text = _FENCE_RE.sub("", raw)
    text = re.sub(r"<(html|body)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</(html|body)>", "", text, flags=re.IGNORECASE)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def normalize_outline(raw: Any) -> dict[str, Any]:
    """Validate & normalize an LLM outline through pydantic schema validation.

    Never trusts the model: bad section/link entries are dropped, the slug is
    derived deterministically when missing, and the result is a frozen,
    immutable snapshot.
    """
    from app.domain.article_html import slugify as _slugify
    from app.schemas.llm import ArticleOutline

    try:
        outline = ArticleOutline.parse_llm(raw)
    except Exception as exc:
        raise ValueError(f"outline failed schema validation: {exc}") from exc

    slug = outline.slug or _slugify(outline.title)
    return {
        "title": outline.title,
        "slug": slug,
        "meta_description": outline.meta_description,
        "search_intent": outline.search_intent,
        "audience": outline.audience,
        "sections": [
            {
                "heading": s.heading,
                "content_brief": s.content_brief,
                "internal_links": [
                    {"title": link.title, "url": link.url, "anchor_text": link.anchor_text}
                    for link in s.internal_links
                ],
                "key": s.key or f"section-{i + 1}",
                "purpose": s.purpose,
                "reader_question": s.reader_question,
                "required_points": list(s.required_points),
                "entities": list(s.entities),
                "evidence": list(s.evidence),
            }
            for i, s in enumerate(outline.sections)
        ],
    }


def build_article_html(
    *,
    title: str,
    slug: str = "",
    sections: list[dict[str, str]],  # [{"heading", "content"}] in order
    internal_links: list[dict[str, str]] | None = None,  # [{"title","url","anchor_text"}]
    intro_paragraph: str = "",
    sanitize: bool = True,
) -> str:
    """Assemble the final article (h1 + optional intro + deduped h2 sections).

    Returns clean semantic HTML: <h1>, <p>, <h2>, <ul>, <a>. No decorative
    separators are added.

    ``sanitize`` defaults to True (safe for untrusted section content). Callers
    that pass already-sanitized content (e.g. the assembler, which receives
    section HTML sanitized when each section completed) should pass False to
    avoid re-sanitizing trusted data.
    """
    parts: list[str] = []
    parts.append(f"<h1>{_escape(title)}</h1>")
    if intro_paragraph and intro_paragraph.strip():
        parts.append(f"<p>{_escape(intro_paragraph.strip())}</p>")

    used_headings: dict[str, int] = {}
    for i, section in enumerate(sections, start=1):
        heading = normalize_article_html(section.get("heading") or "")
        if not heading:
            heading = f"Section {i}"
        heading = _dedupe_heading(heading, used_headings)
        raw = normalize_article_html(section.get("content") or "")
        content = sanitize_html(raw) if sanitize else raw
        if not content:
            continue
        parts.append(f"<h2>{_escape(heading)}</h2>")
        parts.append(content)

    links = _valid_internal_links(internal_links or [])
    if links:
        parts.append("<h2>Related</h2>")
        items = "".join(
            f'<li><a href="{_escape(link["url"])}" target="_blank" rel="noopener">{_escape(link["anchor_text"] or link["title"])}</a></li>'
            for link in links
        )
        parts.append(f"<ul>{items}</ul>")

    return "\n".join(parts)


def _dedupe_heading(heading: str, used: dict[str, int]) -> str:
    key = heading.strip().lower()
    count = used.get(key, 0) + 1
    used[key] = count
    if count > 1:
        return f"{heading} ({count})"
    return heading


def _valid_internal_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only valid, deduplicated internal links."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for link in links:
        url = str(link.get("url") or "").strip()
        if not url.startswith(("http://", "https://", "/")):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "title": str(link.get("title") or ""),
                "url": url,
                "anchor_text": str(link.get("anchor_text") or link.get("title") or ""),
            }
        )
    return out


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def article_word_count(html: str) -> int:
    return word_count_from_html(html)
