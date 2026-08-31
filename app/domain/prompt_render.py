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

# Canonical variable registry: name → Persian description (shown in the UI).
VARIABLE_REGISTRY: dict[str, str] = {
    "project.name": "نام پروژه",
    "project.slug": "شناسه پروژه",
    "project.language": "زبان پروژه",
    "topic.title": "عنوان موضوع",
    "topic.keyword": "کلمه کلیدی موضوع",
    "topic.pillar": "ستون موضوع",
    "topic.cluster": "خوشه موضوع",
    "topic.type": "نوع موضوع",
    "article.title": "عنوان مقاله",
    "article.slug": "اسلاگ مقاله",
    "article.meta_description": "متا توضیحات مقاله",
    "section.heading": "تیتر بخش",
    "section.content_brief": "خلاصه محتوای بخش (brief)",
    "section.position": "شماره ترتیب بخش (۰ = بخش اول مقاله)",
    "retrieved_context": "زمینه بازیابی — بخش‌های مرتبط از مقالات موجود",
    "internal_links": "لینک‌های داخلی پیشنهادی (عنوان + آدرس)",
    "seo_rules": "قوانین سئوی حل‌شده پروژه",
    "internal_linking_rules": "قوانین لینک‌سازی داخلی حل‌شده پروژه",
    "language": "زبان نگارش",
    "raw_output": "خروجی خام مدل (فقط در پرامپت اعتبارسنجی)",
    # image planning prompts
    "prompt_language": "زبان پرامپت تصویری (مستقل از زبان مقاله)",
    "sections": "فهرست بخش‌های مقاله برای برنامه‌ریزی تصویر",
    "style_profile": "پروفایل سبک بصری پروژه (tone/palette/lighting)",
    "max_interior_images": "حداکثر تعداد تصویر داخلی مجاز",
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
    unknown = [name for name in used_variables(content) if name not in VARIABLE_REGISTRY]
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

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in VARIABLE_REGISTRY:
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
