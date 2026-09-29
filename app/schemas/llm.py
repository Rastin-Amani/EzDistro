"""Pydantic schemas for structured LLM responses — the schema-validation gate.

Every structured LLM output passes through these models before use. NEVER
trust arbitrary LLM JSON: structured output → JSON extraction → schema
validation → safe repair/retry. Invalid structures are never silently accepted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


class InternalLink(BaseModel):
    """Intended internal link for a section (from the outline)."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=300)
    anchor_text: str = Field(min_length=1, max_length=120)

    @field_validator("title", "url", "anchor_text", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _strip(value)

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        if value.startswith(("http://", "https://", "/")):
            return value
        raise ValueError("internal link URL must be http(s) or site-relative")


class ArticleSectionPlan(BaseModel):
    """One outline section — the brief the section job must cover."""

    model_config = ConfigDict(extra="ignore")

    heading: str = Field(min_length=1, max_length=120)
    content_brief: str = Field(min_length=1, max_length=2000)
    internal_links: list[InternalLink] = Field(default_factory=list, max_length=5)
    # multilingual-engine brief fields (all optional — old outlines stay valid)
    key: str = Field(default="", max_length=40)
    purpose: str = Field(default="", max_length=1000)
    reader_question: str = Field(default="", max_length=500)
    required_points: list[str] = Field(default_factory=list, max_length=20)
    entities: list[str] = Field(default_factory=list, max_length=30)
    evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("heading", "content_brief", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _strip(value)


class ArticleOutline(BaseModel):
    """Strict, validated article outline.

    Equivalent to {title, slug, sections: [{heading, content_brief,
    internal_links: [{title, url, anchor_text}]}]}. Any other structure is
    rejected or repaired — never silently accepted.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(default="", max_length=200)
    sections: list[ArticleSectionPlan] = Field(min_length=2, max_length=12)
    # multilingual-engine outline fields (optional — old outlines stay valid)
    meta_description: str = Field(default="", max_length=500)
    search_intent: str = Field(default="", max_length=500)
    audience: str = Field(default="", max_length=500)

    @field_validator("title", "slug", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _strip(value)

    @classmethod
    def parse_llm(cls, raw: Any) -> ArticleOutline:
        """Parse untrusted LLM output.

        Malformed section/link entries are dropped (one bad item must not kill
        the whole outline); the remaining structure must still satisfy the
        schema or a ValidationError is raised.
        """
        if not isinstance(raw, dict):
            raise ValueError("LLM outline must be a JSON object")

        sections = []
        if isinstance(raw.get("sections"), list):
            for item in raw["sections"]:
                try:
                    sections.append(ArticleSectionPlan.model_validate(item))
                except Exception:
                    continue
        if len(sections) < 2:
            raise ValueError("LLM outline must contain at least 2 valid sections")

        return cls(
            title=raw.get("title") or "",
            slug=raw.get("slug") or "",
            sections=sections,
        )


class SectionContent(BaseModel):
    """Validated per-section HTML from the LLM."""

    model_config = ConfigDict(extra="ignore")

    html: str = Field(min_length=1)
