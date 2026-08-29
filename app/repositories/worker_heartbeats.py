"""Worker heartbeat repository — liveness registry for worker processes.

Each worker process upserts ONE row (unique `workerId`) every heartbeat
interval. The web reads the rows to show active/stale/offline workers and
their per-session job counts.
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo


class WorkerHeartbeatRepo(BaseRepo):
    collection = "worker_heartbeats"

    def upsert(self, worker_id: str, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.first(filter=f'workerId="{worker_id}"')
        if existing:
            return self.update(existing["id"], data)
        return self.create({"workerId": worker_id, **data})

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_records(filter="", sort="-lastHeartbeatAt", page=1, per_page=limit)
