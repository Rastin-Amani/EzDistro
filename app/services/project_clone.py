"""Duplicate a project's configuration into a freshly created project.

Copies what makes a project *behave* alike: settings (models, namespace/brand,
image config, landing-page product profile), non-site provider connections —
secret ciphertext included, since the same server key decrypts it — and the
ACTIVE prompt versions. Content and research are never copied: topics, articles,
sections, images, index documents and keyword research stay in the source
project. Schedules are site-bound automation, so they stay behind too.

The PUBLISHER connection is deliberately NOT copied: it binds a project to one
specific website. Copying it made the new project an alias of the source site —
the first index/research run then mirrored that site's published posts into the
copy (articles with the source's wordpressPostId, so publishing there would
UPDATE the original site's posts).
"""

from __future__ import annotations

from typing import Any

from app.repositories.integrations import IntegrationRepo
from app.repositories.projects import ProjectSettingsRepo
from app.repositories.prompts import PromptRepo

# PocketBase system fields + the relation we rewrite for the target project.
_SYSTEM_FIELDS = frozenset({"id", "created", "updated", "collectionId", "collectionName", "expand"})


def _copyable_settings(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in _SYSTEM_FIELDS and k != "project"}


def _remap_connection_refs(
    data: dict[str, Any], id_map: dict[str, str], source_ids: set[str]
) -> dict[str, Any]:
    """Point provider settings at the COPIED connections, not the source's ids.

    A setting such as ``imageCoverProvider`` stores a connection id. Copied
    verbatim it keeps naming the source project's connection, which the target
    cannot resolve — every image job then dies with "unknown image provider".
    References to a skipped connection (the publisher) are cleared.
    """
    return {
        key: (id_map.get(value, "") if isinstance(value, str) and value in source_ids else value)
        for key, value in data.items()
    }


def clone_project_configuration(
    pb: Any, *, source_id: str, target_id: str, user_id: str = ""
) -> dict[str, int]:
    """Copy configuration rows source → target. Returns the copied row counts."""
    counts = {"settings": 0, "integrations": 0, "prompts": 0, "schedules": 0}

    integrations = IntegrationRepo(pb)
    source_rows = integrations.list_for_project(source_id)
    id_map: dict[str, str] = {}
    for row in source_rows:
        if (row.get("category") or "") == "publisher":
            continue
        created = integrations.create(
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
        id_map[str(row.get("id") or "")] = created["id"]
        counts["integrations"] += 1

    settings = ProjectSettingsRepo(pb)
    raw = settings.first(filter=f'project="{source_id}"')
    if raw:
        source_ids = {str(row.get("id") or "") for row in source_rows}
        settings.upsert(
            target_id, _remap_connection_refs(_copyable_settings(raw), id_map, source_ids)
        )
        counts["settings"] = 1

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

    # Schedules are site-bound automation (index the site, write, publish to it);
    # with no publisher connection copied they could only produce failing jobs.
    counts["schedules"] = 0

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
