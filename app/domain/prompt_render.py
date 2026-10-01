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

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_UNCLOSED_RE = re.compile(r"\{\{[^{}]*$")


def variable_labels() -> dict[str, str]:
    """Canonical variable registry: name → UI description (English-only)."""
    return {
        "project.name": ("Project name"),
        "project.slug": ("Project slug"),
        "project.language": ("Project language"),
        "topic.title": ("Topic title"),
        "topic.keyword": ("Topic keyword"),
        "topic.pillar": ("Topic pillar"),
        "topic.cluster": ("Topic cluster"),
        "topic.type": ("Topic type"),
        "article.title": ("Article title"),
        "article.slug": ("Article slug"),
        "article.meta_description": ("Article meta description"),
        "article.content": ("Full article content (HTML)"),
        "article.metadata": ("Article metadata (JSON)"),
        "article": ("Full article (title + content)"),
        "section.heading": ("Section heading"),
        "section.content_brief": ("Section content brief"),
        "section.position": ("Section position (0 = the first section)"),
        "section.key": ("Section key (e.g. section-1)"),
        "section.purpose": ("Section purpose"),
        "section.reader_question": ("Reader question this section answers"),
        "section.required_points": ("Required points for the section"),
        "section.entities": ("Section entities"),
        "section.evidence": ("Section evidence requirements"),
        "retrieved_context": ("Retrieved context — related sections from existing articles"),
        "internal_links": ("Suggested internal links (title + URL)"),
        "seo_rules": ("The project's resolved SEO rules"),
        "internal_linking_rules": ("The project's resolved internal linking rules"),
        "language": ("Writing language"),
        "locale": ("Target locale (e.g. en-US, fa-IR)"),
        "country": ("Target country/market"),
        "audience": ("Target audience"),
        "raw_output": ("Raw model output (validation prompt only)"),
        "output_schema": ("Expected JSON schema (output_validation only)"),
        # brand / voice
        "brand_name": ("Brand / site name"),
        "brand_voice": ("Resolved brand voice"),
        "preferred_terminology": ("Preferred terminology"),
        "forbidden_terminology": ("Forbidden terminology"),
        # research / intent
        "search_intent": ("Dominant search intent"),
        "research": ("Research object (JSON)"),
        "search_results": ("Search results evidence"),
        "competitor_pages": ("Competitor pages evidence"),
        "related_queries": ("Related queries"),
        "questions": ("Reader questions"),
        "entities": ("Entities"),
        "essential_questions": ("Essential reader questions"),
        "subtopics": ("Essential subtopics"),
        "content_gaps": ("Content gaps"),
        "original_value_opportunities": ("Original value opportunities"),
        "evidence_requirements": ("Evidence requirements"),
        "site_context": ("Site context"),
        "product_context": ("Brand / product context"),
        "priority_pages": ("Priority pages for internal linking"),
        "topical_clusters": ("Topical clusters"),
        "url_policy": ("URL / slug policy for non-Latin scripts"),
        # section neighbours
        "previous_section": ("Previous section summary"),
        "next_section": ("Following section summary"),
        # QA / repair / refresh
        "qa_issues": ("QA findings (JSON)"),
        "search_console_queries": ("Search Console queries"),
        "search_performance": ("Search performance data"),
        "rankings": ("Current rankings"),
        # image planning prompts
        "prompt_language": ("Image prompt language (independent of the article language)"),
        "sections": ("Article section list for image planning"),
        "style_profile": ("Project visual style profile (tone/palette/lighting)"),
        "max_interior_images": ("Maximum allowed number of interior images"),
        # SEO research engine
        "keywords": ("Research keyword batch (JSON, from Google Ads)"),
        "business_goal": ("Business / content goal in the user's own words"),
        "existing_content": ("Existing site content relevant to the request"),
        "research_summary": ("Aggregated research counts for this run"),
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
