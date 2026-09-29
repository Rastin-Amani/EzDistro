"""Safe prompt template rendering.

Prompts are DATA, never code. Rendering is a pure regex substitution of
`{{ variable }}` tokens against a canonical variable registry:

- only registered variables are ever substituted
- unknown variables FAIL validation before execution (save-time and render-time)
- no template engine, no code execution — prompts are treated as untrusted
- an unclosed `{{` is reported as an error
"""

from __future__ import annotations

import re
from typing import Any

from app.i18n import _

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_UNCLOSED_RE = re.compile(r"\{\{[^{}]*$")


def variable_labels() -> dict[str, str]:
    """Canonical variable registry: name → UI description.

    Called per request so labels follow the active locale (see field_labels).
    """
    return {
        "project.name": _("Project name"),
        "project.slug": _("Project slug"),
        "project.language": _("Project language"),
        "topic.title": _("Topic title"),
        "topic.keyword": _("Topic keyword"),
        "topic.pillar": _("Topic pillar"),
        "topic.cluster": _("Topic cluster"),
        "topic.type": _("Topic type"),
        "article.title": _("Article title"),
        "article.slug": _("Article slug"),
        "article.meta_description": _("Article meta description"),
        "article.content": _("Full article content (HTML)"),
        "article.metadata": _("Article metadata (JSON)"),
        "article": _("Full article (title + content)"),
        "section.heading": _("Section heading"),
        "section.content_brief": _("Section content brief"),
        "section.position": _("Section position (0 = the first section)"),
        "section.key": _("Section key (e.g. section-1)"),
        "section.purpose": _("Section purpose"),
        "section.reader_question": _("Reader question this section answers"),
        "section.required_points": _("Required points for the section"),
        "section.entities": _("Section entities"),
        "section.evidence": _("Section evidence requirements"),
        "retrieved_context": _("Retrieved context — related sections from existing articles"),
        "internal_links": _("Suggested internal links (title + URL)"),
        "seo_rules": _("The project's resolved SEO rules"),
        "internal_linking_rules": _("The project's resolved internal linking rules"),
        "language": _("Writing language"),
        "locale": _("Target locale (e.g. en-US, fa-IR)"),
        "country": _("Target country/market"),
        "audience": _("Target audience"),
        "raw_output": _("Raw model output (validation prompt only)"),
        "output_schema": _("Expected JSON schema (output_validation only)"),
        # brand / voice
        "brand_name": _("Brand / site name"),
        "brand_voice": _("Resolved brand voice"),
        "preferred_terminology": _("Preferred terminology"),
        "forbidden_terminology": _("Forbidden terminology"),
        # research / intent
        "search_intent": _("Dominant search intent"),
        "research": _("Research object (JSON)"),
        "search_results": _("Search results evidence"),
        "competitor_pages": _("Competitor pages evidence"),
        "related_queries": _("Related queries"),
        "questions": _("Reader questions"),
        "entities": _("Entities"),
        "essential_questions": _("Essential reader questions"),
        "subtopics": _("Essential subtopics"),
        "content_gaps": _("Content gaps"),
        "original_value_opportunities": _("Original value opportunities"),
        "evidence_requirements": _("Evidence requirements"),
        "site_context": _("Site context"),
        "product_context": _("Brand / product context"),
        "priority_pages": _("Priority pages for internal linking"),
        "topical_clusters": _("Topical clusters"),
        "url_policy": _("URL / slug policy for non-Latin scripts"),
        # section neighbours
        "previous_section": _("Previous section summary"),
        "next_section": _("Following section summary"),
        # QA / repair / refresh
        "qa_issues": _("QA findings (JSON)"),
        "search_console_queries": _("Search Console queries"),
        "search_performance": _("Search performance data"),
        "rankings": _("Current rankings"),
        # image planning prompts
        "prompt_language": _("Image prompt language (independent of the article language)"),
        "sections": _("Article section list for image planning"),
        "style_profile": _("Project visual style profile (tone/palette/lighting)"),
        "max_interior_images": _("Maximum allowed number of interior images"),
    }


class PromptRenderError(ValueError):
    """Raised when a prompt contains unknown variables or malformed tokens."""

    def __init__(self, unknown: list[str], unclosed: bool = False) -> None:
        self.unknown = unknown
        self.unclosed = unclosed
        parts = []
        if unknown:
            parts.append("unknown variables: " + ", ".join(sorted(unknown)))
        if unclosed:
            parts.append("unclosed {{ token")
        super().__init__("; ".join(parts) or "invalid prompt template")


def used_variables(content: str) -> list[str]:
    """Names of all {{ ... }} tokens in the content (order of appearance)."""
    return _TOKEN_RE.findall(content or "")


def validate_prompt(content: str) -> list[str]:
    """Return unknown variable names; [] when the template is safe.

    Also flags unclosed tokens as unknown via a special marker check.
    """
    if not content:
        return []
    unknown = [name for name in used_variables(content) if name not in variable_labels()]
    return unknown


def render_prompt(content: str, variables: dict[str, Any] | None = None) -> str:
    """Render a prompt template against known variables.

    Raises PromptRenderError when unknown variables or unclosed tokens exist —
    BEFORE anything reaches the LLM.
    """
    if not content:
        return ""
    variables = variables or {}
    unknown: list[str] = []
    known = variable_labels()

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in known:
            unknown.append(name)
            return match.group(0)
        value = variables.get(name)
        if value is None:
            return ""
        return str(value)

    rendered = _TOKEN_RE.sub(_sub, content)
    unclosed = bool(_UNCLOSED_RE.search(rendered)) and "{{" in rendered
    if unknown or unclosed:
        raise PromptRenderError(unknown, unclosed)
    return rendered


def format_internal_links(links: list[dict[str, Any]] | None) -> str:
    """Format link candidates for prompt injection: '- title (url)' lines."""
    if not links:
        return ""
    lines = []
    for link in links:
        title = str(link.get("title") or "")
        url = str(link.get("url") or "")
        if title and url:
            lines.append(f"- {title} ({url})")
    return "\n".join(lines)
