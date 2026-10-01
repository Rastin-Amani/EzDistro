"""SEO research repositories — Google Ads connections, research runs, keywords,
clusters, competitor pages, content gaps, article ideas and SERP observations.

Field names are camelCase (PocketBase convention). Plain CRUD comes from BaseRepo;
the helpers below add the get-or-create / upsert semantics that keep the background
research pipeline resumable and idempotent.

PocketBase (>= 0.23) has no batch-write endpoint, so `create_many` is one request
per row: writes dominate the cost of a large run. Reads are always batched (a
chunked OR filter), which is where the savings actually are.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from pocketbase.errors import ClientResponseError

from app.repositories.base import BaseRepo
from app.repositories.jobs import now_utc, pb_dt

# Rows per OR-filter lookup. Keeps the filter string well under PocketBase's limit.
CHUNK = 200


def q(value: Any) -> str:
    """Render a value as a PocketBase filter string literal."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def chunks(items: Sequence[Any], size: int = CHUNK) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _is_duplicate(exc: Exception) -> bool:
    return isinstance(exc, ClientResponseError) and exc.status in (400, 409)


class _BulkCreateMixin:
    """Create rows, treating unique-constraint violations as already-present."""

    def create_many(self, rows: Iterable[dict[str, Any]], *, ignore_duplicates: bool = True) -> int:
        created = 0
        for row in rows:
            try:
                super().create(row)  # type: ignore[misc]
                created += 1
            except ClientResponseError as exc:
                if ignore_duplicates and _is_duplicate(exc):
                    continue
                raise
        return created


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------
class GoogleAdsConnectionRepo(BaseRepo):
    collection = "google_ads_connections"

    def for_user(self, user_id: str) -> dict[str, Any] | None:
        return self.first(filter=f"user={q(user_id)}", sort="-created")

    def upsert(
        self,
        *,
        user: str,
        google_account_id: str,
        email: str = "",
        display_name: str = "",
        refresh_token_enc: str = "",
        token_metadata: dict[str, Any] | None = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "googleAccountId": google_account_id,
            "email": email,
            "displayName": display_name,
            "status": "connected",
            "lastError": "",
            "lastVerifiedAt": pb_dt(now_utc()),
        }
        if refresh_token_enc:
            payload["refreshTokenEnc"] = refresh_token_enc
        if token_metadata is not None:
            payload["tokenMetadata"] = token_metadata
        existing = self.for_user(user)
        if existing:
            return self.update(existing["id"], payload)
        payload.update({"user": user, "createdBy": created_by or user})
        return self.create(payload)

    def mark_status(self, record_id: str, status: str, error: str = "") -> dict[str, Any]:
        return self.update(record_id, {"status": status, "lastError": error[:500]})

    def mark_verified(self, record_id: str) -> dict[str, Any]:
        return self.update(
            record_id, {"lastVerifiedAt": pb_dt(now_utc()), "status": "connected", "lastError": ""}
        )


class GoogleAdsCustomerRepo(BaseRepo):
    collection = "google_ads_customers"

    def list_for_connection(self, connection_id: str) -> list[dict[str, Any]]:
        return self.list_all(filter=f"connection={q(connection_id)}", sort="descriptiveName")

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.list_all(filter=f"project={q(project_id)}", sort="descriptiveName")

    def get_for_connection(self, connection_id: str, customer_id: str) -> dict[str, Any] | None:
        return self.first(filter=f"connection={q(connection_id)} && customerId={q(customer_id)}")

    def upsert(
        self,
        *,
        connection: str,
        customer_id: str,
        descriptive_name: str = "",
        currency_code: str = "",
        time_zone: str = "",
        manager_customer_id: str = "",
        is_manager: bool = False,
        accessible: bool = True,
        project: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connection": connection,
            "customerId": customer_id,
            "descriptiveName": descriptive_name,
            "currencyCode": currency_code,
            "timeZone": time_zone,
            "managerCustomerId": manager_customer_id,
            "isManager": is_manager,
            "accessible": accessible,
        }
        if project:
            payload["project"] = project
        if metadata is not None:
            payload["metadata"] = metadata
        existing = self.get_for_connection(connection, customer_id)
        if existing:
            return self.update(existing["id"], payload)
        return self.create(payload)


# ---------------------------------------------------------------------------
# Research runs
# ---------------------------------------------------------------------------
class ResearchRunRepo(BaseRepo):
    collection = "research_runs"

    @staticmethod
    def fingerprint(targeting: dict[str, Any], seeds: list[dict[str, Any]]) -> str:
        """Stable identity of a research request (targeting + seed set).

        Two runs with the same fingerprint ask the same question, so the second
        one can reuse the first instead of paying Google Ads again. The project
        is deliberately NOT part of it: the caller scopes the lookup by project.
        """
        payload = {
            "targeting": {
                key: targeting.get(key)
                for key in (
                    "country",
                    "language",
                    "locale",
                    "network",
                    "includeAdultKeywords",
                    "languageId",
                    "geoTargets",
                    "locationId",
                )
            },
            "seeds": sorted(
                f"{seed.get('seedType') or ''}:{(seed.get('normalizedValue') or seed.get('value') or '').strip().lower()}"
                for seed in seeds
            ),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def create_run(
        self,
        *,
        project: str,
        name: str,
        research_type: str,
        config: dict[str, Any],
        targeting: dict[str, Any],
        fingerprint: str = "",
        job: str = "",
        connection: str = "",
        customer_id: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        return self.create(
            {
                "project": project,
                "name": name,
                "researchType": research_type,
                "status": "pending",
                "currentStage": "validate",
                "config": config,
                "targeting": targeting,
                "fingerprint": fingerprint,
                "progress": 0,
                "stageState": {},
                "counts": {},
                "job": job,
                "connection": connection,
                "customerId": customer_id,
                "createdBy": created_by,
            }
        )

    def for_fingerprint(self, project_id: str, fingerprint: str) -> dict[str, Any] | None:
        if not fingerprint:
            return None
        return self.first(
            filter=f"project={q(project_id)} && fingerprint={q(fingerprint)}", sort="-created"
        )

    def list_for_project(
        self, project_id: str, *, status: str = "", page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        f = f"project={q(project_id)}"
        if status:
            f += f" && status={q(status)}"
        return self.list_records(filter=f, sort="-created", page=page, per_page=per_page)

    def count_for_project(self, project_id: str) -> int:
        return self.count(filter=f"project={q(project_id)}")

    def active_for_project(self, project_id: str) -> dict[str, Any] | None:
        return self.first(
            filter=(f'project={q(project_id)} && (status="pending" || status="running")'),
            sort="-created",
        )

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status}
        if status == "running":
            payload["startedAt"] = pb_dt(now_utc())
        if status in ("completed", "failed", "cancelled", "partial"):
            payload["completedAt"] = pb_dt(now_utc())
            if status == "completed":
                payload["progress"] = 100
        if error_code or error_message:
            payload["errorCode"] = error_code
            payload["errorMessage"] = error_message[:500]
        if error_details is not None:
            payload["errorDetails"] = error_details
        return self.update(run_id, payload)

    def set_stage(
        self,
        run_id: str,
        stage: str,
        *,
        progress: int | None = None,
        stage_state: dict[str, Any] | None = None,
        counts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"currentStage": stage}
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        if stage_state is not None:
            payload["stageState"] = stage_state
        if counts is not None:
            payload["counts"] = counts
        return self.update(run_id, payload)


class ResearchSeedRepo(_BulkCreateMixin, BaseRepo):
    collection = "research_seeds"

    def replace_for_run(self, run_id: str, seeds: Iterable[dict[str, Any]]) -> int:
        self.delete_matching(filter=f"run={q(run_id)}")
        rows = [{**seed, "run": seed.get("run") or run_id} for seed in seeds]
        return self.create_many(rows)

    def list_for_run(self, run_id: str, seed_type: str = "") -> list[dict[str, Any]]:
        f = f"run={q(run_id)}"
        if seed_type:
            f += f" && seedType={q(seed_type)}"
        return self.list_all(filter=f, sort="seedType,value")


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------
class KeywordRepo(_BulkCreateMixin, BaseRepo):
    collection = "keywords"

    def identity(self, project_id: str, language: str, location_id: str) -> list[dict[str, Any]]:
        """All keyword rows for one targeting identity (project+language+location)."""
        return self.list_all(
            filter=(
                f"project={q(project_id)} && language={q(language)} && locationId={q(location_id)}"
            )
        )

    def map_by_normalized(
        self, project_id: str, language: str, location_id: str, keys: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Batched lookup: normalizedKeyword -> record, for the given keys only."""
        found: dict[str, dict[str, Any]] = {}
        for chunk in chunks(list(keys)):
            ors = " || ".join(f"normalizedKeyword={q(k)}" for k in chunk)
            rows = self.list_all(
                filter=(
                    f"project={q(project_id)} && language={q(language)} "
                    f"&& locationId={q(location_id)} && ({ors})"
                )
            )
            for row in rows:
                found[row.get("normalizedKeyword", "")] = row
        return found

    def map_by_ids(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Batched lookup by record id (one query per CHUNK ids)."""
        found: dict[str, dict[str, Any]] = {}
        for chunk in chunks(list(ids)):
            ors = " || ".join(f"id={q(i)}" for i in chunk)
            for row in self.list_all(filter=f"({ors})"):
                found[row["id"]] = row
        return found

    def ensure_many(
        self,
        *,
        project: str,
        language: str,
        location_id: str,
        rows: Sequence[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Get-or-create keyword rows. `rows` needs normalizedKeyword plus
        displayKeyword/locale/locationName/source."""
        by_key: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            key = row.get("normalizedKeyword", "")
            if not key or key in seen:
                continue
            seen.add(key)
            pending.append(row)

        existing = self.map_by_normalized(
            project, language, location_id, [r["normalizedKeyword"] for r in pending]
        )
        by_key.update(existing)
        for row in pending:
            key = row["normalizedKeyword"]
            if key in by_key:
                continue
            payload = {
                "project": project,
                "language": language,
                "locationId": location_id,
                "source": row.get("source", ""),
                "displayKeyword": row.get("displayKeyword") or key,
                "locale": row.get("locale", ""),
                "locationName": row.get("locationName", ""),
                "normalizedKeyword": key,
            }
            try:
                by_key[key] = self.create(payload)
            except ClientResponseError as exc:
                if not _is_duplicate(exc):
                    raise
                found = self.first(
                    filter=(
                        f"project={q(project)} && language={q(language)} "
                        f"&& locationId={q(location_id)} && normalizedKeyword={q(key)}"
                    )
                )
                if found is None:
                    raise
                by_key[key] = found
        return by_key


class KeywordMetricRepo(_BulkCreateMixin, BaseRepo):
    collection = "keyword_metrics"

    def upsert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        """Rows are keyed by (run, keyword). Batched read, then update or create."""
        if not rows:
            return 0
        run_id = rows[0]["run"]
        by_keyword = {r["keyword"]: r for r in rows}
        existing: dict[str, dict[str, Any]] = {}
        keys = list(by_keyword)
        for chunk in chunks(keys):
            ors = " || ".join(f"keyword={q(k)}" for k in chunk)
            for row in self.list_all(filter=f"run={q(run_id)} && ({ors})"):
                existing[row.get("keyword", "")] = row
        written = 0
        for keyword_id, row in by_keyword.items():
            current = existing.get(keyword_id)
            payload = {k: v for k, v in row.items() if k not in ("run", "keyword")}
            payload["run"] = run_id
            payload["keyword"] = keyword_id
            if current:
                self.update(current["id"], payload)
            else:
                self.create(payload)
            written += 1
        return written

    def list_for_run(
        self,
        run_id: str,
        *,
        filter: str = "",
        sort: str = "-avgMonthlySearches",
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        f = f"run={q(run_id)}"
        if filter:
            f += f" && ({filter})"
        return self.list_records(filter=f, sort=sort, page=page, per_page=per_page)

    def count_for_run(self, run_id: str, *, filter: str = "") -> int:
        f = f"run={q(run_id)}"
        if filter:
            f += f" && ({filter})"
        return self.count(filter=f)

    def top_for_run(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f"run={q(run_id)}", sort="-avgMonthlySearches", per_page=min(500, limit)
        )

    def for_keyword(self, run_id: str, keyword_id: str) -> dict[str, Any] | None:
        return self.first(filter=f"run={q(run_id)} && keyword={q(keyword_id)}")

    def stats_for_run(self, run_id: str) -> dict[str, float]:
        """Aggregate demand stats with one paged pass (bounded per_page)."""
        total = 0
        volume = 0
        high_competition = 0
        rows = 0
        page = 1
        while True:
            batch = self.list_records(
                filter=f"run={q(run_id)}",
                sort="-avgMonthlySearches",
                page=page,
                per_page=500,
            )
            if not batch:
                break
            for row in batch:
                rows += 1
                vol = int(row.get("avgMonthlySearches") or 0)
                volume += vol
                if vol > 0:
                    total += 1
                if str(row.get("competition") or "").upper() == "HIGH":
                    high_competition += 1
            if len(batch) < 500:
                break
            page += 1
            # ponytail: full scan for demand stats; swap for a PB aggregate view if
            # runs grow past ~100k keywords and this shows up in profiling.
            if page > 500:
                break
        return {
            "keywords": rows,
            "keywords_with_volume": total,
            "total_monthly_volume": volume,
            "high_ads_competition": high_competition,
        }


class KeywordVolumeRepo(_BulkCreateMixin, BaseRepo):
    collection = "keyword_volumes"

    def replace_for_keyword(
        self, run_id: str, keyword_id: str, points: Iterable[tuple[int, int, int]]
    ) -> int:
        self.delete_matching(filter=f"run={q(run_id)} && keyword={q(keyword_id)}")
        return self.create_many(
            {
                "run": run_id,
                "keyword": keyword_id,
                "year": year,
                "month": month,
                "monthlySearches": count,
            }
            for year, month, count in points
        )

    def for_keyword(self, run_id: str, keyword_id: str) -> list[dict[str, Any]]:
        return self.list_all(
            filter=f"run={q(run_id)} && keyword={q(keyword_id)}", sort="year,month"
        )


# ---------------------------------------------------------------------------
# Clusters, competitor pages, gaps
# ---------------------------------------------------------------------------
class ClusterRepo(_BulkCreateMixin, BaseRepo):
    collection = "clusters"

    def list_for_run(self, run_id: str, *, per_page: int = 200) -> list[dict[str, Any]]:
        return self.list_records(filter=f"run={q(run_id)}", sort="-size", per_page=per_page)

    def map_for_run(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.list_all(filter=f"run={q(run_id)}")}

    def count_for_run(self, run_id: str) -> int:
        return self.count(filter=f"run={q(run_id)}")

    def for_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.list_all(filter=f"project={q(project_id)}", sort="name")


class CompetitorPageRepo(_BulkCreateMixin, BaseRepo):
    collection = "competitor_pages"

    def get_for_canonical(self, project_id: str, canonical_url: str) -> dict[str, Any] | None:
        return self.first(filter=f"project={q(project_id)} && canonicalUrl={q(canonical_url)}")

    def list_for_run(
        self,
        run_id: str,
        *,
        status: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        f = f"run={q(run_id)}"
        if status:
            f += f" && status={q(status)}"
        return self.list_records(filter=f, sort="-wordCount", page=page, per_page=per_page)

    def count_for_run(self, run_id: str, *, status: str = "") -> int:
        f = f"run={q(run_id)}"
        if status:
            f += f" && status={q(status)}"
        return self.count(filter=f)

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.list_all(filter=f"project={q(project_id)}", sort="-wordCount")


class ContentGapRepo(_BulkCreateMixin, BaseRepo):
    collection = "content_gaps"

    def replace_for_run(self, run_id: str, rows: Iterable[dict[str, Any]]) -> int:
        self.delete_matching(filter=f"run={q(run_id)}")
        return self.create_many(rows)

    def list_for_run(
        self,
        run_id: str,
        *,
        gap_type: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        f = f"run={q(run_id)}"
        if gap_type:
            f += f" && gapType={q(gap_type)}"
        return self.list_records(filter=f, sort="-score", page=page, per_page=per_page)

    def count_for_run(self, run_id: str, *, gap_type: str = "") -> int:
        f = f"run={q(run_id)}"
        if gap_type:
            f += f" && gapType={q(gap_type)}"
        return self.count(filter=f)


# ---------------------------------------------------------------------------
# Article ideas
# ---------------------------------------------------------------------------
class ArticleIdeaRepo(_BulkCreateMixin, BaseRepo):
    collection = "article_ideas"

    def list_for_run(
        self,
        run_id: str,
        *,
        status: str = "",
        action: str = "",
        filter: str = "",
        sort: str = "-opportunityScore",
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        f = self._run_filter(run_id, status=status, action=action, extra=filter)
        return self.list_records(filter=f, sort=sort, page=page, per_page=per_page)

    def count_for_run(
        self, run_id: str, *, status: str = "", action: str = "", filter: str = ""
    ) -> int:
        return self.count(
            filter=self._run_filter(run_id, status=status, action=action, extra=filter)
        )

    def all_for_run(self, run_id: str, *, status: str = "") -> list[dict[str, Any]]:
        f = f"run={q(run_id)}"
        if status:
            f += f" && status={q(status)}"
        return self.list_all(filter=f, sort="-opportunityScore")

    def list_for_project(
        self, project_id: str, *, status: str = "", per_page: int = 500
    ) -> list[dict[str, Any]]:
        f = f"project={q(project_id)}"
        if status:
            f += f" && status={q(status)}"
        return self.list_records(filter=f, sort="-created", per_page=per_page)

    def summarize_run(self, run_id: str) -> dict[str, int]:
        base = f"run={q(run_id)}"
        out: dict[str, int] = {"total": self.count(filter=base)}
        for action in ("generate", "update", "expand", "support", "reject"):
            out[action] = self.count(filter=f"{base} && action={q(action)}")
        for status in ("proposed", "accepted", "roadmap", "rejected", "merged", "generated"):
            out[status] = self.count(filter=f"{base} && status={q(status)}")
        return out

    @staticmethod
    def _run_filter(run_id: str, *, status: str = "", action: str = "", extra: str = "") -> str:
        f = f"run={q(run_id)}"
        if status:
            f += f" && status={q(status)}"
        if action:
            f += f" && action={q(action)}"
        if extra:
            f += f" && ({extra})"
        return f


# ---------------------------------------------------------------------------
# SERP
# ---------------------------------------------------------------------------
class SerpQueryRepo(BaseRepo):
    collection = "serp_queries"

    def get_observation(
        self, project_id: str, keyword: str, provider: str, locale: str, location: str
    ) -> dict[str, Any] | None:
        return self.first(
            filter=(
                f"project={q(project_id)} && keyword={q(keyword)} && provider={q(provider)} "
                f"&& locale={q(locale)} && location={q(location)}"
            )
        )

    def upsert(
        self,
        *,
        project: str,
        run: str,
        keyword: str,
        provider: str,
        locale: str,
        location: str,
        device: str = "desktop",
        result_count: int = 0,
        features: Any = None,
        questions: Any = None,
        related_searches: Any = None,
        raw: Any = None,
        observed_at: Any = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "keyword": keyword,
            "provider": provider,
            "locale": locale,
            "location": location,
            "device": device,
            "resultCount": result_count,
            "features": features or [],
            "questions": questions or [],
            "relatedSearches": related_searches or [],
            "raw": raw or {},
            "observedAt": pb_dt(observed_at or now_utc()),
        }
        existing = self.get_observation(project, keyword, provider, locale, location)
        if existing:
            if run:
                payload["run"] = run
            return self.update(existing["id"], payload)
        payload.update({"project": project, "run": run})
        return self.create(payload)

    def list_for_run(
        self, run_id: str, *, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f"run={q(run_id)}", sort="-observedAt", page=page, per_page=per_page
        )

    def count_for_run(self, run_id: str) -> int:
        return self.count(filter=f"run={q(run_id)}")


class SerpResultRepo(_BulkCreateMixin, BaseRepo):
    collection = "serp_results"

    def replace_for_query(self, query_id: str, results: Iterable[dict[str, Any]]) -> int:
        self.delete_matching(filter=f"query={q(query_id)}")
        return self.create_many({**row, "query": query_id} for row in results)

    def list_for_query(self, query_id: str) -> list[dict[str, Any]]:
        return self.list_all(filter=f"query={q(query_id)}", sort="position")
