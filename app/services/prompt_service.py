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
    ) -> dict[str, Any]:
        project = config.project
        topic = topic or {}
        article = article or {}
        section = section or {}
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
            "section.heading": section.get("heading") or "",
            "section.content_brief": section.get("content_brief")
            or section.get("contentBrief")
            or "",
            "section.position": str(section.get("position") or ""),
            "retrieved_context": retrieval_context or "",
            "internal_links": format_internal_links(internal_links),
            "language": config.language,
            "raw_output": raw_output,
        }
        # rule prompts are data too: resolve them recursively with the same context
        # (save-time validation guarantees only known variables appear in them)
        rules: dict[str, Any] = {}
        for key, raw in (
            ("seo_rules", config.prompt("seo_rules")),
            ("internal_linking_rules", config.prompt("internal_linking")),
        ):
            rules[key] = render_prompt(raw, base) if raw else ""
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
