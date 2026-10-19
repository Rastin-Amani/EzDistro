"""Choosing a WordPress category for an article — the rules, without I/O.

The site's own taxonomy is authoritative: an existing category is reused
whenever it matches, by name or by a shared meaningful word with the article's
topic. A new name is only proposed when nothing matches, which is what keeps a
site that already organizes itself organized.

Pure functions only: the LLM call and the WordPress requests live in
:mod:`app.services.wp_categories`.
"""

from __future__ import annotations

import re

MAX_NAME_LENGTH = 60
MAX_NEW_NAME_WORDS = 4

# Words that carry no grouping signal in a category name or a keyword.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "best",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "guide",
        "guides",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "the",
        "to",
        "vs",
        "what",
        "when",
        "where",
        "which",
        "why",
        "with",
        "your",
        "you",
    }
)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_name(value: str) -> str:
    """Case/space-folded comparison key (accents preserved: they are meaningful)."""
    return " ".join(str(value or "").split()).strip().casefold()


def _tokens(value: str) -> set[str]:
    return {
        w
        for w in (m.group(0).casefold() for m in _WORD.finditer(value or ""))
        if w not in _STOPWORDS
    }


def match_by_name(name: str, categories: list[dict[str, object]]) -> dict[str, object] | None:
    """The category whose name (or slug) equals `name`, ignoring case."""
    wanted = normalize_name(name)
    if not wanted:
        return None
    for row in categories:
        if normalize_name(str(row.get("name") or "")) == wanted:
            return row
        if normalize_name(str(row.get("slug") or "")) == wanted:
            return row
    return None


def best_token_match(text: str, categories: list[dict[str, object]]) -> dict[str, object] | None:
    """The existing category that shares the most meaningful words with `text`.

    "Gym Management Software Pricing" against "Gym Management" scores 2 —
    enough to reuse the site's own category instead of inventing a new one.
    """
    wanted = _tokens(text)
    if not wanted:
        return None
    best: dict[str, object] | None = None
    best_score = 0
    for row in categories:
        shared = _tokens(str(row.get("name") or "")) & wanted
        if not shared:
            continue
        score = len(shared) * 10 - len(_tokens(str(row.get("name") or "")))
        if score > best_score:
            best, best_score = row, score
    return best


def clean_category_name(raw: str) -> str:
    """A short single-line category name, or "" when the input is unusable."""
    name = " ".join(str(raw or "").split())
    # strip wrapping quotes/markdown a model sometimes adds
    name = name.strip().strip("\"'`*_").strip()
    name = name.rstrip(".:;,")
    if not name or len(name) > MAX_NAME_LENGTH:
        return ""
    if len(name.split()) > MAX_NEW_NAME_WORDS:
        return ""
    if normalize_name(name) in ("uncategorized", "uncategorised", "general", "other", "misc"):
        return ""
    return name
