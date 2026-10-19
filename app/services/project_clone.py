"""Duplicate a project's configuration into a freshly created project.

Copies what makes a project *behave* alike: settings (models, namespace/brand,
image config, landing-page product profile), provider connections — secret
ciphertext included, since the same server key decrypts it — the ACTIVE prompt
versions and the schedules. Content and research are never copied: topics,
articles, sections, images, index documents and keyword research stay in the
source project.
"""

from __future__ import annotations

from typing import Any

from app.repositories.integrations import IntegrationRepo
from app.repositories.projects import ProjectSettingsRepo
from app.repositories.prompts import PromptRepo
from app.repositories.schedules import ScheduleRepo

# PocketBase system fields + the relation we rewrite for the target project.
_SYSTEM_FIELDS = frozenset({"id", "created", "updated", "collectionId", "collectionName", "expand"})


def _copyable_settings(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in _SYSTEM_FIELDS and k != "project"}


def clone_project_configuration(
    pb: Any, *, source_id: str, target_id: str, user_id: str = ""
) -> dict[str, int]:
    """Copy configuration rows source → target. Returns the copied row counts."""
    counts = {"settings": 0, "integrations": 0, "prompts": 0, "schedules": 0}

    settings = ProjectSettingsRepo(pb)
    raw = settings.first(filter=f'project="{source_id}"')
    if raw:
        settings.upsert(target_id, _copyable_settings(raw))
        counts["settings"] = 1

    integrations = IntegrationRepo(pb)
    for row in integrations.list_for_project(source_id):
        integrations.create(
            project=target_id,
            category=row.get("category") or "",
            provider=row.get("provider") or "",
            display_name=row.get("displayName") or "",
            model=row.get("model") or "",
            configuration=row.get("configuration") or {},
            secrets_enc=row.get("secretsEnc") or "",
            enabled=bool(row.get("enabled")),
            created_by=user_id,
        )
        counts["integrations"] += 1

    # Only the ACTIVE version of each prompt type — history stays in the source.
    prompts = PromptRepo(pb)
    for row in prompts.list_records(
        filter=f'project="{source_id}" && active=true', sort="type,name", per_page=200
    ):
        prompts.save_version(
            project_id=target_id,
            ptype=row.get("type") or "",
            name=row.get("name") or "default",
            content=row.get("content") or "",
            updated_by=user_id,
            variables=row.get("variables") or {},
        )
        counts["prompts"] += 1

    schedules = ScheduleRepo(pb)
    for row in schedules.list_for_project(source_id):
        kind = row.get("kind") or "index"
        schedules.create(
            project=target_id,
            name=row.get("name") or kind,
            kind=kind,
            interval_minutes=int(row.get("intervalMinutes") or 1440),
            payload=row.get("payload") or {},
            enabled=bool(row.get("enabled")),
        )
        counts["schedules"] += 1

    return counts


def clone_summary(counts: dict[str, int]) -> str:
    """Human-readable tail for the "project created" toast (English only)."""
    parts = []
    for key, label in (
        ("integrations", "connections"),
        ("prompts", "prompts"),
        ("schedules", "schedules"),
        ("settings", "settings"),
    ):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    return ", ".join(parts)
