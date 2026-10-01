"""Pure keyword/text helpers for SEO research (no I/O, no dependencies).

Deliberately language-neutral: NFKC + case folding + Unicode letter/digit
tokenization works for Latin, Persian/Arabic and Turkish alike, so no language
gets English-only stemming rules applied to it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

# Very small stopword sets used only to derive head terms / cluster labels.
# ponytail: extend per locale when a locale shows poor cluster labels.
STOPWORDS: frozenset[str] = frozenset(
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
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "از",
        "برای",
        "با",
        "به",
        "در",
        "را",
        "که",
        "چی",
        "چه",
        "این",
        "آن",
        "هم",
        "یا",
        "و",
        "یک",
        "می",
        "های",
        "ها",
        "است",
        "بود",
    }
)

# Modifier words that change *intent* rather than topic — used by the clusterer to
# decide that two queries sharing a head term may still deserve separate articles.
INTENT_MODIFIERS: frozenset[str] = frozenset(
    {
        "best",
        "top",
        "vs",
        "versus",
        "compare",
        "comparison",
        "alternative",
        "alternatives",
        "review",
        "reviews",
        "price",
        "pricing",
        "cost",
        "cheap",
        "free",
        "software",
        "tool",
        "tools",
        "app",
        "apps",
        "buy",
        "demo",
        "trial",
        "guide",
        "tutorial",
        "how",
        "what",
        "why",
        "example",
        "examples",
        "template",
        "checklist",
        "statistics",
        "vs.",
    }
)


def normalize_keyword(text: str) -> str:
    """Canonical form used for de-duplication and storage keys (never displayed)."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    chars = [c if (c.isalnum() or c.isspace()) else " " for c in folded]
    return " ".join("".join(chars).split())


def tokens(text: str) -> list[str]:
    """Unicode-aware token list (letters/digits), normalized."""
    normalized = normalize_keyword(text)
    return [t for t in normalized.split(" ") if t]


def content_tokens(text: str) -> list[str]:
    """Tokens without stopwords — the topic-bearing part of a query."""
    return [t for t in tokens(text) if t not in STOPWORDS]


def shingles(text: str, size: int = 2) -> set[str]:
    """Word n-grams: cheap phrase similarity without an embedding model."""
    words = content_tokens(text)
    if len(words) < size:
        return set(words)
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(left: Iterable[str], right: Iterable[str]) -> float:
    """Overlap relative to the smaller set (short queries inside longer ones)."""
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def similarity(left: str, right: str) -> float:
    """0..1 lexical similarity: token overlap blended with phrase overlap.

    Used by deterministic clustering and duplicate detection. Embedding
    similarity is used *in addition* when the project has embeddings configured.
    """
    if not left or not right:
        return 0.0
    if normalize_keyword(left) == normalize_keyword(right):
        return 1.0
    token_score = containment(content_tokens(left), content_tokens(right))
    phrase_score = containment(shingles(left), shingles(right))
    return round(0.6 * token_score + 0.4 * phrase_score, 4)


def head_term(text: str) -> str:
    """The most specific (longest, non-modifier) token — a decent cluster label."""
    words = [w for w in content_tokens(text) if w not in INTENT_MODIFIERS]
    if not words:
        words = content_tokens(text) or tokens(text)
    return max(words, key=len) if words else ""


def _demo() -> None:
    assert normalize_keyword("  Gym   Management  SOFTWARE! ") == "gym management software"
    assert normalize_keyword("Gym-Management") == "gym management"
    assert content_tokens("the best gym software") == ["gym", "software"]
    assert shingles("gym management software") == {"gym management", "management software"}
    assert containment(["gym", "software"], ["gym"]) == 1.0
    assert similarity("gym software", "gym software pricing") > 0.5
    assert similarity("gym software", "restaurant accounting") < 0.2
    assert similarity("gym software", "gym software") == 1.0
    assert head_term("best gym management software") in {"management", "software"}
    assert tokens("کلاس مدیریت باشگاه")[:2] == ["کلاس", "مدیریت"]


if __name__ == "__main__":
    _demo()
    print("keywords ok")
