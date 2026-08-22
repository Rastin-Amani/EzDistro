"""Prompts repository — append-style versioned prompts.

Each save inserts a NEW row (version+1) and deactivates the previous active row,
so full history is preserved. Resolution: active project row → active global row
(project unset).
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

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


class PromptRepo(BaseRepo):
    collection = "prompts"

    def list_for_project(
        self, project_id: str | None, ptype: str | None = None
    ) -> list[dict[str, Any]]:
        parts = []
        if project_id:
            parts.append(f'project="{project_id}"')
        if ptype:
            parts.append(f'type="{ptype}"')
        filter = " && ".join(parts) if parts else ""
        return self.list_records(filter=filter, sort="type,name,-version")

    def _active_row(self, project_id: str, ptype: str, name: str) -> dict[str, Any] | None:
        proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
        return self.first(filter=f'{proj_filter} && type="{ptype}" && name="{name}" && active=true')

    def resolve(self, project_id: str, ptype: str, name: str = "default") -> str:
        """Active project prompt first, then active global default; "" when none."""
        for candidate in (
            self._active_row(project_id, ptype, name),
            self._active_row("", ptype, name),
        ):
            if candidate and candidate.get("content"):
                return candidate["content"]
        return ""

    def resolve_all(
        self, project_id: str, ptypes: list[str], name: str = "default"
    ) -> dict[str, str]:
        """Resolve many prompt types in TWO queries (project rows + global rows),
        instead of one query pair per type."""
        # NOTE: no `type ~ ".*"` here — PB's regex `~` doesn't match select
        # fields, which silently zeroed every prompt row (→ generic fallbacks).
        suffix = f' && name="{name}" && active=true' if name else " && active=true"
        project_rows = self.list_records(filter=f'project="{project_id}"{suffix}', per_page=200)
        global_rows = self.list_records(filter=f'project=""{suffix}', per_page=200)
        by_type: dict[str, str] = {}
        for rows in (project_rows, global_rows):
            for row in rows:
                ptype = row.get("type") or ""
                if ptype in ptypes and ptype not in by_type and row.get("content"):
                    by_type[ptype] = row["content"]
        return {pt: by_type.get(pt, "") for pt in ptypes}

    def save_version(
        self,
        *,
        project_id: str | None,
        ptype: str,
        name: str,
        content: str,
        updated_by: str = "",
        variables: dict[str, Any] | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Insert a new version (append). `activate` deactivates the previous active row."""
        if ptype not in PROMPT_TYPES:
            raise ValueError(f"invalid prompt type: {ptype}")
        proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
        project_value = project_id if project_id else ""

        active = self._active_row(project_id or "", ptype, name)
        latest = self.first(
            filter=f'{proj_filter} && type="{ptype}" && name="{name}"', sort="-version"
        )
        version = int((latest or {}).get("version") or 0) + 1

        if active and activate:
            self.update(active["id"], {"active": False})
        return self.create(
            {
                "project": project_value,
                "type": ptype,
                "name": name,
                "content": content,
                "version": version,
                "active": activate,
                "variables": variables or {},
                "updatedBy": updated_by,
            }
        )

    def activate_version(self, project_id: str, ptype: str, version_id: str) -> dict[str, Any]:
        """Activate a version; deactivate all others of the same (project, type, name)."""
        target = self.get(version_id)
        if not target or target.get("type") != ptype:
            raise ValueError("version not found")
        proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
        others = self.list_records(
            filter=f'{proj_filter} && type="{ptype}" && name="default" && active=true', per_page=10
        )
        for other in others:
            if other["id"] != version_id and other.get("active"):
                self.update(other["id"], {"active": False})
        return self.update(version_id, {"active": True})

    def duplicate_version(
        self, project_id: str, ptype: str, version_id: str, author: str = ""
    ) -> dict[str, Any]:
        """Create a NEW (inactive) version from an existing one — safe to edit."""
        source = self.get(version_id)
        if not source or source.get("type") != ptype:
            raise ValueError("version not found")
        return self.save_version(
            project_id=project_id,
            ptype=ptype,
            name="default",
            content=source.get("content") or "",
            updated_by=author,
            variables={
                "duplicatedFrom": version_id,
                "used": (source.get("variables") or {}).get("used", []),
            },
            activate=False,
        )

    def history(
        self, project_id: str, ptype: str, name: str = "default", limit: int = 10
    ) -> list[dict[str, Any]]:
        proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
        return self.list_records(
            filter=f'{proj_filter} && type="{ptype}" && name="{name}"',
            sort="-version",
            page=1,
            per_page=limit,
        )
