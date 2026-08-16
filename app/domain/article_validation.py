"""Article & section validation — deterministic, no LLM involved.

- SectionValidator: per-section output gate (structural HTML, forbidden
  patterns, no markdown fences, no h1).
- ArticleValidator: whole-article gate run by the assembler (title, slug,
  sections, content, structure, internal links, length).
Validation errors are structured and exposed in the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.domain.chunker import strip_html, word_count_from_html

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    ("script", r"<script", "script tags are forbidden"),
    ("style", r"<style", "style tags are forbidden"),
    ("iframe", r"<iframe", "iframe tags are forbidden"),
    ("event_handler", r"\son\w+\s*=", "inline event handlers are forbidden"),
    ("javascript_url", r"javascript:", "javascript: URLs are forbidden"),
    ("markdown_fence", r"```|~~~", "markdown fences are forbidden"),
    ("raw_html", r"<(html|body)\b", "raw <html>/<body> wrappers are forbidden"),
]

ALLOWED_TOP_LEVEL = {
    "h2",
    "h3",
    "h4",
    "p",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "b",
    "i",
    "a",
    "blockquote",
    "code",
    "pre",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "br",
    "hr",
}


@dataclass
class ValidationIssue:
    code: str
    message: str
    section: str | None = None  # section position (1-based) or heading

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "section": self.section}


@dataclass
class ValidationReport:
    ok: bool = False
    issues: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats,
        }


class _TagBalanceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            return
        # tolerate mismatched closers but report imbalance
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            self.stack.pop()
            self.errors.append(f"mismatched closing tag </{tag}>")


def check_html_structure(html: str) -> list[str]:
    parser = _TagBalanceParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:  # malformed HTML
        return [f"HTML parse error: {exc}"]
    if parser.stack:
        return [f"unclosed tags: {', '.join(parser.stack[:5])}"]
    return parser.errors


class SectionValidator:
    """Output gate for a single generated section."""

    def validate(self, html: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not html or not html.strip():
            return [ValidationIssue("empty", "section content is empty")]

        for code, pattern, message in FORBIDDEN_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                issues.append(ValidationIssue(code, message))

        if re.search(r"<h1\b", html, re.IGNORECASE):
            issues.append(ValidationIssue("h1_in_section", "sections must not contain <h1>"))

        for error in check_html_structure(html):
            issues.append(ValidationIssue("html_structure", error))

        if word_count_from_html(html) < 10:
            issues.append(ValidationIssue("too_short", "section is too short (< 10 words)"))

        return issues


class ArticleValidator:
    """Whole-article gate run by the assembler before review state."""

    def __init__(self, min_words: int = 300, keyword: str = "") -> None:
        self.min_words = max(1, int(min_words))
        self.keyword = (keyword or "").strip().lower()

    def validate(
        self,
        *,
        title: str,
        slug: str,
        outline: dict[str, Any] | None,
        sections: list[dict[str, Any]],
        html: str,
        forbidden_patterns: list[tuple[str, str, str]] | None = None,
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        patterns = forbidden_patterns or FORBIDDEN_PATTERNS

        # identity
        if not title or not title.strip():
            issues.append(ValidationIssue("missing_title", "article title is missing"))
        if not slug or not slug.strip():
            issues.append(ValidationIssue("missing_slug", "article slug is missing"))

        # sections
        if not sections:
            issues.append(ValidationIssue("no_sections", "article has no sections"))
        for i, section in enumerate(sections, start=1):
            heading = str(section.get("heading") or "").strip()
            content = str(section.get("content") or "").strip()
            if not heading:
                issues.append(
                    ValidationIssue("empty_heading", f"section {i} has no heading", str(i))
                )
            if not content:
                issues.append(ValidationIssue("empty_section", f"section {i} is empty", str(i)))
            elif word_count_from_html(content) < 10:
                issues.append(
                    ValidationIssue("section_too_short", f"section {i} is too short", str(i))
                )

        # whole-article structure
        if html:
            for error in check_html_structure(html):
                issues.append(ValidationIssue("html_structure", error))
            for code, pattern, message in patterns:
                if re.search(pattern, html, re.IGNORECASE):
                    issues.append(ValidationIssue(code, message))

        # whole-article content checks (duplicate headings, broken links, suspicious)
        if html:
            _add_content_checks(issues, html)

        # configured SEO requirements: keyword placement
        if self.keyword:
            _add_keyword_checks(issues, title, html, self.keyword)

        # length
        words = word_count_from_html(html)
        if words < self.min_words:
            issues.append(
                ValidationIssue(
                    "too_short", f"article is {words} words; minimum is {self.min_words}"
                )
            )

        # intended internal links must survive assembly
        intended = _collect_intended_links(outline)
        if intended:
            for link in intended:
                url = link.get("url") or ""
                if url and url not in html:
                    issues.append(
                        ValidationIssue(
                            "missing_internal_link",
                            f"intended internal link missing from article: {link.get('title') or url}",
                        )
                    )

        return ValidationReport(
            ok=not issues,
            issues=issues,
            stats={"words": words, "sections": len(sections), "min_words": self.min_words},
        )


def _collect_intended_links(outline: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not outline or not isinstance(outline, dict):
        return []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in outline.get("sections") or []:
        for link in section.get("internal_links") or []:
            url = str(link.get("url") or "").rstrip("/")
            if url and url not in seen:
                seen.add(url)
                links.append(link)
    return links


def _add_content_checks(issues: list[ValidationIssue], html: str) -> None:
    """Duplicate headings, broken internal links, suspicious output."""
    headings = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.IGNORECASE | re.DOTALL)
    cleaned = [re.sub(r"<[^>]+>", "", h).strip().lower() for h in headings]
    seen: dict[str, int] = {}
    for h in cleaned:
        if not h:
            continue
        seen[h] = seen.get(h, 0) + 1
        if seen[h] == 2:
            issues.append(ValidationIssue("duplicate_heading", f"duplicate H2 heading: {h[:60]}"))
            break

    for url in re.findall(r'<a[^>]+href="([^"]+)"', html, re.IGNORECASE):
        stripped = url.strip()
        if stripped.startswith(("http://", "https://", "/", "#", "mailto:")):
            continue
        issues.append(
            ValidationIssue("broken_internal_link", f"invalid link target: {stripped[:80]}")
        )

    if "{{" in html or "}}" in html:
        issues.append(
            ValidationIssue("suspicious_output", "unrendered template tokens remain in the article")
        )
    suspicious = re.findall(r"\b(TODO|FIXME|Lorem ipsum|lorem ipsum|PLACEHOLDER)\b", html)
    if suspicious:
        issues.append(
            ValidationIssue(
                "suspicious_output",
                f"placeholder text present: {', '.join(sorted(set(suspicious)))}",
            )
        )


def _add_keyword_checks(issues: list[ValidationIssue], title: str, html: str, keyword: str) -> None:
    """Configured SEO requirements — keyword placement checks."""
    title_l = (title or "").lower()
    if keyword not in title_l:
        issues.append(
            ValidationIssue("keyword_in_title", f"keyword «{keyword}» is missing from the title")
        )
    first_para = _first_paragraph(html).lower()
    if keyword not in title_l and keyword not in first_para:
        issues.append(
            ValidationIssue(
                "keyword_in_intro", f"keyword «{keyword}» is missing from title and first paragraph"
            )
        )
    headings = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.IGNORECASE | re.DOTALL)
    if not any(keyword in re.sub(r"<[^>]+>", "", h).lower() for h in headings):
        issues.append(
            ValidationIssue("keyword_in_headings", f"keyword «{keyword}» appears in no heading")
        )
    if strip_html(html).lower().count(keyword) == 0:
        issues.append(
            ValidationIssue("keyword_absent", f"keyword «{keyword}» never appears in the article")
        )


def _first_paragraph(html: str) -> str:
    match = re.search(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"<[^>]+>", "", match.group(1)) if match else ""
