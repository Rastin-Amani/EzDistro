"""Retrieval subsystem schemas — normalized result objects + options."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalOptions:
    """Configurable retrieval behaviour (UI-exposed values)."""

    candidate_count: int = 20  # vector pool size
    similarity_threshold: float | None = None  # minimum vector score
    rerank: bool = False  # optional reranking
    rerank_top_n: int = 8  # final candidates after reranking


@dataclass
class RetrievalResult:
    """One normalized retrieval result — the ONLY shape domain code consumes."""

    document_id: str
    title: str
    source_url: str
    snippet: str  # content passage (trimmed)
    vector_score: float
    rerank_score: float | None = None
    final_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.final_score:
            self.final_score = (
                self.rerank_score if self.rerank_score is not None else self.vector_score
            )


@dataclass
class LinkCandidate:
    """Structured internal-link candidate."""

    title: str
    url: str
    relevance_score: float

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "relevance_score": self.relevance_score}


@dataclass
class RetrievalContext:
    """Compact, configurable context for the LLM prompt (never full documents)."""

    query: str
    links: list[LinkCandidate] = field(default_factory=list)
    passages: list[str] = field(default_factory=list)
    used_chars: int = 0
    results: list[RetrievalResult] = field(default_factory=list)

    def link_dicts(self) -> list[dict[str, Any]]:
        return [link.to_dict() for link in self.links]

    def format_for_prompt(self) -> str:
        """Human-readable compact context block for LLM prompts."""
        parts: list[str] = []
        if self.links:
            parts.append("## مقالات موجود در سایت (لینک‌سازی داخلی)")
            for link in self.links:
                parts.append(f"- {link.title} ({link.url}) — {link.relevance_score:.2f}")
        if self.passages:
            parts.append("## بخش‌های مرتبط از مقالات موجود")
            for passage in self.passages:
                parts.append(f"- {passage}")
        return "\n".join(parts)
