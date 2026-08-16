"""App settings repository — global defaults (singleton record keyed "default")."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

GLOBAL_LLM_DEFAULTS: dict[str, Any] = {
    "outline": {
        "provider": "openai_compat",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "max_tokens": 4096,
        "timeout": 120,
    },
    "section": {
        "provider": "openai_compat",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "max_tokens": 4096,
        "timeout": 120,
    },
    "meta": {"provider": "", "model": ""},
    "review": {"provider": "", "model": ""},
}


class AppSettingsRepo(BaseRepo):
    collection = "app_settings"

    def get_defaults(self) -> dict[str, Any]:
        record = self.first(filter='key="default"')
        value = (record or {}).get("value") or {}
        llm = value.get("llm") or {}
        merged = {
            role: {**GLOBAL_LLM_DEFAULTS[role], **(llm.get(role) or {})}
            for role in GLOBAL_LLM_DEFAULTS
        }
        return {"llm": merged, **{k: v for k, v in value.items() if k != "llm"}}

    def set_llm_defaults(self, llm: dict[str, Any]) -> dict[str, Any]:
        record = self.first(filter='key="default"')
        value = dict((record or {}).get("value") or {})
        value["llm"] = llm
        if record:
            return self.update(record["id"], {"value": value})
        return self.create({"key": "default", "value": value})
