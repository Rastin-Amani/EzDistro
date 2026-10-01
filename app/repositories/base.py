"""Base PocketBase repository — thin data access shared by all entity repos.

All repositories speak camelCase (PocketBase field convention). Queries are always
paginated; large collections are never loaded into memory at once.
"""

from __future__ import annotations

from collections.abc import Iterable
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

    def get_many(self, record_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [r for r in (self.get(i) for i in record_ids) if r]

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        return record_to_dict(self._coll().create(data))

    def update(self, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return record_to_dict(self._coll().update(record_id, data))

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
        """
        params: dict[str, Any] = {"perPage": per_page}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        if fields:
            params["fields"] = fields
        return [record_to_dict(r) for r in self._coll().get_full_list(query_params=params)]

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
