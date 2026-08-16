"""Schedules repository — per-project recurring triggers (interval based)."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo
from app.repositories.jobs import now_utc, pb_dt


class ScheduleRepo(BaseRepo):
    collection = "schedules"

    def create(
        self,
        *,
        project: str,
        name: str,
        kind: str,
        interval_minutes: int,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return super().create(
            {
                "project": project,
                "name": name,
                "kind": kind,
                "enabled": True,
                "intervalMinutes": max(1, int(interval_minutes)),
                "nextRunAt": pb_dt(now_utc()),
                "payload": payload or {},
            }
        )

    def due(self, before: Any = None) -> list[dict[str, Any]]:
        if before is None:
            before = now_utc()
        return self.list_records(
            filter=f'enabled=true && nextRunAt <= "{pb_dt(before)}"', sort="nextRunAt", per_page=100
        )

    def mark_run(self, schedule_id: str, next_run_at: Any) -> None:
        self.update(schedule_id, {"lastRunAt": pb_dt(now_utc()), "nextRunAt": pb_dt(next_run_at)})

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        return self.update(schedule_id, {"enabled": enabled})

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.list_records(filter=f'project="{project_id}"', sort="name")
