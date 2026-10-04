"""Base PocketBase repository — thin data access shared by all entity repos.

All repositories speak camelCase (PocketBase field convention). Queries are always
paginated; large collections are never loaded into memory at once.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pocketbase import PocketBase
from pocketbase.errors import ClientResponseError
from pocketbase.models import Record


def record_to_dict(record: Record | dict[str, Any] | None) -> dict[str, Any]:
    """Convert a PB Record to a plain dict (snake→camel untouched: we use camelCase)."""
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    return {k: v for k, v in vars(record).items() if not k.startswith("_")}


def _now_ts() -> str:
    """PocketBase date format: ``2026-10-04 12:00:00.000Z`` (UTC)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, ClientResponseError) and exc.status == 404


class BaseRepo:
    collection: str = ""

    def __init__(self, pb: PocketBase) -> None:
        self._pb = pb

    # -- low-level ------------------------------------------------------------------
    def _coll(self) -> Any:
        return self._pb.collection(self.collection)

    def get(self, record_id: str) -> dict[str, Any] | None:
        try:
            return record_to_dict(self._coll().get_one(record_id))
        except ClientResponseError as exc:
            if _is_not_found(exc):
                return None
            raise

    def get_fields(self, record_id: str, fields: str) -> dict[str, Any] | None:
        """Single record, projected to `fields` — skips large unneeded blobs.

        PocketBase supports nested projections (e.g. ``config.clustering``), so a
        caller can fetch the few sub-keys it renders while dropping hundred-KB
        JSON payloads living beside them.
        """
        try:
            return record_to_dict(self._coll().get_one(record_id, {"fields": fields}))
        except ClientResponseError as exc:
            if _is_not_found(exc):
                return None
            raise

    def get_many(self, record_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [r for r in (self.get(i) for i in record_ids) if r]

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        # The live PocketBase keeps legacy `date` system fields that cannot carry
        # onCreate/onUpdate flags (PB strips them), so the app writes the
        # timestamps itself. Callers may pass real values (imports/backfills).
        now = _now_ts()
        payload = {
            **data,
            "created": data.get("created") or now,
            "updated": data.get("updated") or now,
        }
        return record_to_dict(self._coll().create(payload))

    def update(self, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        payload = {**data, "updated": data.get("updated") or _now_ts()}
        return record_to_dict(self._coll().update(record_id, payload))

    def delete(self, record_id: str) -> None:
        self._coll().delete(record_id)

    # -- querying -------------------------------------------------------------------
    def list_records(
        self,
        *,
        filter: str = "",
        sort: str = "-created",
        page: int = 1,
        per_page: int = 25,
        expand: str = "",
        fields: str = "",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"sort": sort, "perPage": per_page}
        if filter:
            params["filter"] = filter
        if expand:
            params["expand"] = expand
        if fields:
            params["fields"] = fields
        return [record_to_dict(r) for r in self._coll().get_list(page, per_page, params).items]

    def list_all(
        self, *, filter: str = "", sort: str = "", per_page: int = 500, fields: str = ""
    ) -> list[dict[str, Any]]:
        """Safely page through an entire result set (bounded per-page, no full load).

        `fields` projects specific columns (PocketBase `fields` param) — use it when
        scanning large collections whose bodies must not be loaded into memory.

        `batch=` (not a `perPage` query param) is what controls the page size:
        the SDK's ``get_full_list`` uses its own ``batch`` argument and overrides
        any ``perPage`` passed in ``query_params`` — leaving it at the SDK's
        default of 100 made every full scan page 100 rows at a time.
        """
        params: dict[str, Any] = {}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        if fields:
            params["fields"] = fields
        return [
            record_to_dict(r)
            for r in self._coll().get_full_list(batch=per_page, query_params=params)
        ]

    def delete_matching(self, *, filter: str) -> int:
        """Delete every record matching `filter`. Used to replace child rows
        (keyword volumes, SERP results, gaps) idempotently on a resumed run."""
        ids = [r["id"] for r in self.list_all(filter=filter)]
        for record_id in ids:
            self.delete(record_id)
        return len(ids)

    def first(self, *, filter: str = "", sort: str = "") -> dict[str, Any] | None:
        params: dict[str, Any] = {"sort": sort or "-created", "perPage": 1}
        try:
            result = self._coll().get_first_list_item(filter, params)
        except ClientResponseError as exc:
            if _is_not_found(exc):
                return None
            raise
        return record_to_dict(result) if result else None

    def count(self, *, filter: str = "") -> int:
        params: dict[str, Any] = {"perPage": 1}
        if filter:
            params["filter"] = filter
        return self._coll().get_list(1, 1, params).total_items
