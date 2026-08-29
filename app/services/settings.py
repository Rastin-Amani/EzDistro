"""ProjectConfig — one typed, validated view of everything a job needs about a project.

Assembled once per job run: project record + flat merged settings + resolved prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.prompts import PromptRepo

# Prompt types consumed by the writer pipeline (project row → global default).
PROMPT_TYPES = (
    "outline_system",
    "outline_user",
    "section_system",
    "section_user",
    "seo_rules",
    "internal_linking",
    "brand_voice",
    "validation",
)


LLM_ROLES = ("outline", "section", "meta", "review")


@dataclass
class ProjectConfig:
    project: dict[str, Any]
    settings: dict[str, Any]
    prompts: dict[str, str] = field(default_factory=dict)
    global_llm: dict[str, Any] = field(default_factory=dict)  # role → defaults

    # -- identity -------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.project["id"]

    @property
    def slug(self) -> str:
        return str(self.project.get("slug") or self.project["id"])

    @property
    def language(self) -> str:
        return str(self.project.get("language") or "fa")

    @property
    def timezone(self) -> str:
        return str(self.project.get("timezone") or "Asia/Tehran")

    # -- flat settings accessors ----------------------------------------------------
    @property
    def llm(self) -> dict[str, Any]:
        """Legacy single-LLM view (kept for compatibility)."""
        return {
            "provider": self.settings.get("defaultLlmProvider") or "openai_compat",
            "model": self.settings.get("defaultLlmModel") or "gpt-4o-mini",
        }

    def role_llm(self, role: str) -> dict[str, Any]:
        """Resolved model config for a role.

        Priority: project override → global default → legacy fields.
        """
        if role not in LLM_ROLES:
            raise ValueError(f"unknown LLM role: {role}")
        key = lambda suffix: f"{role}{suffix}"  # noqa: E731
        global_cfg = self.global_llm.get(role) or {}
        provider = (
            self.settings.get(key("Provider"))
            or global_cfg.get("provider")
            or self.settings.get("defaultLlmProvider")
            or "openai_compat"
        )
        model = (
            self.settings.get(key("Model"))
            or global_cfg.get("model")
            or self.settings.get("defaultLlmModel")
            or "gpt-4o-mini"
        )
        return {
            "provider": provider,
            "model": model,
            "temperature": float(
                self.settings.get(key("Temperature")) or global_cfg.get("temperature") or 0.7
            ),
            "max_tokens": int(
                self.settings.get(key("MaxTokens")) or global_cfg.get("max_tokens") or 4096
            ),
            "timeout": float(
                self.settings.get(key("Timeout")) or global_cfg.get("timeout") or 120.0
            ),
            "retry": self.settings.get(key("Retry")) or {},
        }

    @property
    def outline_llm(self) -> dict[str, Any]:
        return self.role_llm("outline")

    @property
    def section_llm(self) -> dict[str, Any]:
        return self.role_llm("section")

    @property
    def meta_llm(self) -> dict[str, Any]:
        return self.role_llm("meta")

    @property
    def review_llm(self) -> dict[str, Any]:
        return self.role_llm("review")

    @property
    def embedding(self) -> dict[str, Any]:
        return {
            "provider": self.settings.get("embeddingProvider") or "openai_compat",
            "model": self.settings.get("embeddingModel") or "text-embedding-3-small",
            "dimensions": int(self.settings.get("embeddingDimensions") or 1536),
        }

    @property
    def chunking(self) -> dict[str, Any]:
        return {
            "size": int(self.settings.get("chunkSize") or 500),
            "overlap": int(self.settings.get("chunkOverlap") or 100),
            "strategy": self.settings.get("separatorStrategy") or "auto",
            "max_chunks": int(self.settings.get("maxChunkCount") or 0),
        }

    @property
    def retrieval(self) -> dict[str, Any]:
        return {
            "top_k": int(self.settings.get("retrievalTopK") or 20),
            "similarity_threshold": float(self.settings.get("similarityThreshold") or 0.0),
            "rerank_enabled": bool(self.settings.get("rerankingEnabled")),
            "reranker_provider": self.settings.get("rerankerProvider") or "cohere_compat",
            "rerank_model": self.settings.get("rerankerModel") or "rerank-v4.0",
            "rerank_top_n": int(self.settings.get("rerankerTopN") or 8),
        }

    @property
    def context(self) -> dict[str, Any]:
        return {
            "max_links": int(self.settings.get("contextMaxLinks") or 5),
            "max_passages": int(self.settings.get("contextMaxPassages") or 5),
            "max_chars": int(self.settings.get("contextMaxChars") or 4000),
        }

    @property
    def generation(self) -> dict[str, Any]:
        return {
            "section_concurrency": int(self.settings.get("generationConcurrency") or 2),
            "min_article_words": int(self.settings.get("minArticleWords") or 300),
        }

    @property
    def retry_policy(self) -> dict[str, Any]:
        policy = self.settings.get("retryPolicy") or {}
        return {
            "max_attempts": int(policy.get("max_attempts") or 3),
            "backoff_base": int(policy.get("backoff_base") or 30),
            "backoff_max": int(policy.get("backoff_max") or 3600),
        }

    @property
    def publishing(self) -> dict[str, Any]:
        mode = self.settings.get("publishingMode") or "draft"
        return {"mode": mode, "wp_status": mode}

    @property
    def autosave(self) -> dict[str, Any]:
        return self.settings.get("autosave") or {"enabled": False}

    @property
    def auto_publish(self) -> dict[str, Any]:
        cfg = self.settings.get("autoPublish") or {}
        return {
            "enabled": bool(cfg.get("enabled")),
            "min_score": max(0, int(cfg.get("min_score") or 90)),
            "max_attempts": max(1, int(cfg.get("max_attempts") or 3)),
        }

    @property
    def indexing(self) -> dict[str, Any]:
        return self.settings.get("indexing") or {}

    # -- prompts --------------------------------------------------------------------
    def prompt(self, ptype: str, fallback: str = "") -> str:
        return self.prompts.get(ptype) or fallback

    def prompt_with_extra(self, ptype: str, extra: str) -> str:
        base = self.prompt(ptype)
        return f"{base}\n\n{extra}" if base else extra

    # -- builder --------------------------------------------------------------------
    @classmethod
    def load(cls, pb: Any, project_id: str) -> ProjectConfig:
        from app.repositories.app_settings import AppSettingsRepo

        project = ProjectRepo(pb).get(project_id)
        if not project:
            raise ValueError(f"project not found: {project_id}")
        settings_record = ProjectSettingsRepo(pb).get_for_project(project_id)
        prompts = PromptRepo(pb).resolve_all(project_id, list(PROMPT_TYPES))
        defaults = AppSettingsRepo(pb).get_defaults()
        return cls(
            project=project,
            settings=settings_record,
            prompts=prompts,
            global_llm=defaults.get("llm") or {},
        )
