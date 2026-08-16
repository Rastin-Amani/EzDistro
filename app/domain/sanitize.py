"""HTML sanitization for article content displayed in the admin UI.

The LLM output is never trusted: only an explicit allowlist survives.
"""

from __future__ import annotations

import bleach

_ALLOWED_TAGS = [
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
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
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]

_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}

_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}


def sanitize_html(raw: str) -> str:
    """Sanitize untrusted HTML (LLM output / WP content) for display in the admin UI."""
    if not raw:
        return ""
    return bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


def sanitize_plaintext(raw: str) -> str:
    """Strip everything, keep only visible text."""
    if not raw:
        return ""
    return bleach.clean(raw, tags=[], strip=True)
