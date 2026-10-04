"""Prompt management service.

- builds the variable context for a project/topic/article/section
- validates + renders prompt templates (safe, data-driven)
- interactive prompt tester: render → LLM → latency/usage/validation result
  WITHOUT modifying any stored version
"""

from __future__ import annotations

import time
from typing import Any

from app.domain.prompt_render import (
    PromptRenderError,
    format_internal_links,
    render_prompt,
    used_variables,
    validate_prompt,
)
from app.providers.base import GenerationParams
from app.providers.registry import ProviderRegistry
from app.repositories.prompts import PromptRepo
from app.services.settings import ProjectConfig


def _fmt_list(value: Any) -> str:
    """Render a list var for prompts: newline bullets; strings pass through."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    return str(value)


class PromptService:
    def __init__(self, pb: Any, registry: ProviderRegistry | None = None) -> None:
        self._pb = pb
        self._registry = registry or ProviderRegistry(pb)

    # ---------------------------------------------------------------------------
    # Context
    # ---------------------------------------------------------------------------
    def build_context(
        self,
        config: ProjectConfig,
        *,
        topic: dict[str, Any] | None = None,
        article: dict[str, Any] | None = None,
        section: dict[str, Any] | None = None,
        retrieval_context: str = "",
        internal_links: list[dict[str, Any]] | None = None,
        raw_output: str = "",
        output_schema: str = "",
        research: str = "",
        search_intent: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from app.domain.product_profile import format_product_profile

        project = config.project
        topic = topic or {}
        article = article or {}
        section = section or {}
        settings = config.settings or {}
        profile_text = format_product_profile(settings.get("productProfile"))
        article_content = (
            article.get("finalHtml")
            or article.get("generatedContent")
            or article.get("content")
            or ""
        )
        article_metadata = article.get("metadata") or article.get("metaDescription") or ""
        if isinstance(article_metadata, dict):
            import json as _json

            article_metadata = _json.dumps(article_metadata, ensure_ascii=False)
        base = {
            "project.name": project.get("name") or "",
            "project.slug": project.get("slug") or "",
            "project.language": project.get("language") or "",
            "topic.title": topic.get("title") or "",
            "topic.keyword": topic.get("keyword") or "",
            "topic.pillar": topic.get("pillar") or "",
            "topic.cluster": topic.get("cluster") or "",
            "topic.type": topic.get("type") or "",
            "article.title": article.get("title") or "",
            "article.slug": article.get("slug") or "",
            "article.meta_description": article.get("metaDescription") or "",
            "article.content": article_content,
            "article.metadata": article_metadata,
            "article": ((article.get("title") or "") + "\n\n" + article_content).strip(),
            "section.heading": section.get("heading") or "",
            "section.content_brief": section.get("content_brief")
            or section.get("contentBrief")
            or "",
            "section.position": str(section.get("position") or ""),
            "section.key": str(
                section.get("key") or f"section-{int(section.get('position') or 0) + 1}"
            ),
            "section.purpose": section.get("purpose") or "",
            "section.reader_question": section.get("reader_question")
            or section.get("readerQuestion")
            or "",
            "section.required_points": _fmt_list(
                section.get("required_points") or section.get("requiredPoints")
            ),
            "section.entities": _fmt_list(section.get("entities")),
            "section.evidence": _fmt_list(section.get("evidence")),
            "retrieved_context": retrieval_context or "",
            "internal_links": format_internal_links(internal_links),
            "language": config.language,
            "locale": settings.get("targetLocale") or project.get("locale") or "",
            "country": settings.get("targetCountry") or "",
            "audience": settings.get("targetAudience") or "",
            "raw_output": raw_output,
            "output_schema": output_schema,
            "brand_name": settings.get("brandName") or project.get("name") or "",
            "preferred_terminology": settings.get("preferredTerminology") or "",
            "forbidden_terminology": settings.get("forbiddenTerminology") or "",
            "search_intent": search_intent or topic.get("searchIntent") or "",
            "research": research or "",
            "search_results": "",
            "competitor_pages": "",
            "related_queries": "",
            "questions": "",
            "entities": _fmt_list(topic.get("entities")),
            "essential_questions": "",
            "subtopics": "",
            "content_gaps": "",
            "original_value_opportunities": "",
            "evidence_requirements": "",
            "site_context": project.get("description") or "",
            "product_context": settings.get("productContext") or profile_text,
            "product_profile": profile_text,
            "landing_page_url": settings.get("landingPageUrl") or "",
            "priority_pages": "",
            "topical_clusters": "",
            "url_policy": settings.get("urlPolicy") or "",
            "previous_section": "",
            "next_section": "",
            "qa_issues": "",
            "search_console_queries": "",
            "search_performance": "",
            "rankings": "",
            "keywords": "",
            "business_goal": "",
            "existing_content": "",
            "research_summary": "",
        }
        if extra:
            for k, v in extra.items():
                base[k] = v if isinstance(v, str) else _fmt_list(v) if isinstance(v, list) else v
        # rule prompts are data too: resolve them recursively with the same context
        # (save-time validation guarantees only known variables appear in them).
        # seo_rules prefers the new seo_content_contract, falling back to legacy.
        contract = config.prompt("seo_content_contract") or config.prompt("seo_rules")
        rules: dict[str, Any] = {}
        for key, raw in (
            ("seo_rules", contract),
            ("internal_linking_rules", config.prompt("internal_linking")),
            ("brand_voice", config.prompt("brand_voice")),
        ):
            rules[key] = render_prompt(raw, base) if raw else ""
        # The product profile grounds every prompt that reads brand_voice /
        # product_context, without requiring per-project prompt edits.
        if profile_text:
            rules["brand_voice"] = (
                f"{rules['brand_voice']}\n\n{profile_text}".strip()
                if rules["brand_voice"]
                else profile_text
            )
        return {**base, **rules}

    # ---------------------------------------------------------------------------
    # Validate / render
    # ---------------------------------------------------------------------------
    def validate_content(self, content: str) -> list[str]:
        """Unknown variable names; [] when safe (validation BEFORE save/run)."""
        return validate_prompt(content)

    def render(self, content: str, context: dict[str, Any]) -> str:
        """Render a template; raises PromptRenderError on unknown vars."""
        return render_prompt(content, context)

    def resolve_active(self, project_id: str, ptype: str) -> dict[str, Any] | None:
        return PromptRepo(self._pb)._active_row(project_id, ptype, "default")

    # ---------------------------------------------------------------------------
    # Versioning
    # ---------------------------------------------------------------------------
    def save(self, project_id: str, ptype: str, content: str, author: str = "") -> dict[str, Any]:
        unknown = self.validate_content(content)
        if unknown:
            raise PromptRenderError(unknown)
        variables = {"used": used_variables(content)}
        return PromptRepo(self._pb).save_version(
            project_id=project_id,
            ptype=ptype,
            name="default",
            content=content,
            updated_by=author,
            variables=variables,
        )

    def activate(self, project_id: str, ptype: str, version_id: str) -> dict[str, Any]:
        return PromptRepo(self._pb).activate_version(project_id, ptype, version_id)

    def duplicate(
        self, project_id: str, ptype: str, version_id: str, author: str = ""
    ) -> dict[str, Any]:
        return PromptRepo(self._pb).duplicate_version(project_id, ptype, version_id, author)

    # ---------------------------------------------------------------------------
    # Interactive tester — never touches stored versions
    # ---------------------------------------------------------------------------
    async def test(
        self,
        config: ProjectConfig,
        *,
        ptype: str,
        content: str,
        topic: dict[str, Any] | None = None,
        retrieval_context: str = "",
        internal_links: list[dict[str, Any]] | None = None,
        model_role: str = "outline",
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Render → generate → validate. Returns the full result payload."""
        context = self.build_context(
            config,
            topic=topic,
            article=None,
            retrieval_context=retrieval_context,
            internal_links=internal_links,
        )
        rendered = self.render(content, context)

        role_cfg = config.role_llm(model_role)
        llm = self._registry.get_llm_provider(
            config.project,
            config.settings,
            role=model_role,
            role_config=role_cfg,
        )
        params = GenerationParams(
            temperature=float(role_cfg.get("temperature") or 0.7),
            max_tokens=int(role_cfg.get("max_tokens") or max_tokens),
            timeout=float(role_cfg.get("timeout") or 120.0),
        )
        started = time.monotonic()
        try:
            result = await llm.generate(system=None, user=rendered, params=params)
            latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
            response = result.text
            usage = result.usage
            model = result.model
        finally:
            close = getattr(llm, "aclose", None)
            if close is not None:
                await close()

        return {
            "rendered": rendered,
            "model": model,
            "latency_ms": latency_ms,
            "response": response,
            "usage": usage,
            "validation": self._validate_response(ptype, response),
        }

    def _validate_response(self, ptype: str, response: str) -> dict[str, Any]:
        if ptype in ("outline_user", "outline_system"):
            try:
                from app.domain.article_html import normalize_outline
                from app.domain.parsing import extract_json

                outline = normalize_outline(extract_json(response))
                return {"ok": True, "issues": [], "sections": len(outline.get("sections") or [])}
            except Exception as exc:
                return {
                    "ok": False,
                    "issues": [{"code": "outline_schema", "message": str(exc)[:300]}],
                    "sections": 0,
                }
        if ptype in ("section_user", "section_system"):
            from app.domain.article_validation import SectionValidator

            issues = SectionValidator().validate(response)
            return {"ok": not issues, "issues": [i.to_dict() for i in issues]}
        return {"ok": None, "issues": [], "sections": 0}
