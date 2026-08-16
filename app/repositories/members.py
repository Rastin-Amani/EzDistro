"""Project memberships repository — authorization for non-admin users."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

MEMBER_ROLES = ("owner", "admin", "editor", "viewer")


class MemberRepo(BaseRepo):
    collection = "project_members"

    def add(self, *, project: str, user: str, role: str = "viewer") -> dict[str, Any]:
        if role not in MEMBER_ROLES:
            raise ValueError(f"invalid member role: {role}")
        existing = self.first(filter=f'project="{project}" && user="{user}"')
        if existing:
            return self.update(existing["id"], {"role": role})
        return super().create({"project": project, "user": user, "role": role})

    def remove(self, project: str, user: str) -> None:
        existing = self.first(filter=f'project="{project}" && user="{user}"')
        if existing:
            self.delete(existing["id"])

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.list_records(filter=f'project="{project_id}"', sort="created")

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self.list_records(filter=f'user="{user_id}"', sort="created")

    def role_of(self, project: str, user: str) -> str:
        membership = self.first(filter=f'project="{project}" && user="{user}"')
        return (membership or {}).get("role") or ""
