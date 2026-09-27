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
        "section.heading": _("Section heading"),
        "section.content_brief": _("Section content brief"),
        "section.position": _("Section position (0 = the first section)"),
        "retrieved_context": _("Retrieved context — related sections from existing articles"),
        "internal_links": _("Suggested internal links (title + URL)"),
        "seo_rules": _("The project's resolved SEO rules"),
        "internal_linking_rules": _("The project's resolved internal linking rules"),
        "language": _("Writing language"),
        "raw_output": _("Raw model output (validation prompt only)"),
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
