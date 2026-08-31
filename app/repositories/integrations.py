"""Integrations repository — configurable providers per category.

Categories: llm | embedding | reranker | vector_store | publisher.
Secrets live ONLY in `secretsEnc` (Fernet ciphertext); `configuration` JSON is
non-secret (base_url, model, username, …).
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

INTEGRATION_CATEGORIES = ("llm", "embedding", "reranker", "vector_store", "publisher", "image")
HEALTH_STATUSES = ("unknown", "healthy", "degraded", "unhealthy")


class IntegrationRepo(BaseRepo):
    collection = "integrations"

    def create(
        self,
        *,
        project: str,
        category: str,
        provider: str,
        display_name: str,
        configuration: dict[str, Any] | None = None,
        secrets_enc: str = "",
        enabled: bool = True,
        created_by: str = "",
    ) -> dict[str, Any]:
        if category not in INTEGRATION_CATEGORIES:
            raise ValueError(f"invalid integration category: {category}")
        return super().create(
            {
                "project": project,
                "category": category,
                "provider": provider,
                "displayName": display_name,
                "configuration": configuration or {},
                "secretsEnc": secrets_enc,
                "enabled": enabled,
                "healthStatus": "unknown",
                "lastTestedAt": "",
                "createdBy": created_by,
            }
        )

    def list_for_project(
        self, project_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if category:
            f += f' && category="{category}"'
        return self.list_records(filter=f, sort="category,displayName")

    def get_active(self, project_id: str, category: str) -> dict[str, Any] | None:
        return self.first(
            filter=f'project="{project_id}" && category="{category}" && enabled=true',
            sort="created",
        )

    def set_enabled(self, record_id: str, enabled: bool) -> dict[str, Any]:
        return self.update(record_id, {"enabled": enabled})

    def mark_health(self, record_id: str, status: str, tested_at: Any = None) -> dict[str, Any]:
        if status not in HEALTH_STATUSES:
            raise ValueError(f"invalid health status: {status}")
        payload: dict[str, Any] = {"healthStatus": status}
        if tested_at is not None:
            from app.repositories.jobs import now_utc, pb_dt

            payload["lastTestedAt"] = pb_dt(tested_at or now_utc())
        return self.update(record_id, payload)
