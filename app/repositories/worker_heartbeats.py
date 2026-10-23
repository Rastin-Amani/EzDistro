"""Worker heartbeat repository — liveness registry for worker processes.

Each worker process upserts ONE row (unique `workerId`) every heartbeat
interval. The web reads the rows to show active/stale/offline workers and
their per-session job counts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.repositories.base import BaseRepo
from app.repositories.jobs import now_utc, pb_dt

# Workers beat every ~15s; 45s tolerates two missed beats before the worker is
# considered offline. Shared by the Workers page and the retry route so both
# answer the same question the same way.
LIVE_AFTER_SECONDS = 45


class WorkerHeartbeatRepo(BaseRepo):
    collection = "worker_heartbeats"

    def upsert(self, worker_id: str, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.first(filter=f'workerId="{worker_id}"')
        if existing:
            return self.update(existing["id"], data)
        return self.create({"workerId": worker_id, **data})

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_records(filter="", sort="-lastHeartbeatAt", page=1, per_page=limit)

    def any_live(self) -> bool:
        """Is at least one worker process still heartbeating?"""
        cutoff = pb_dt(now_utc() - dt.timedelta(seconds=LIVE_AFTER_SECONDS))
        return self.first(filter=f'lastHeartbeatAt > "{cutoff}"') is not None
