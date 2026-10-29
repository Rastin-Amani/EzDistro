"""Search-engine notification (IndexNow) — job handler + queueing helpers.

Design notes:

- The notification is a JOB, not an inline call inside publishing: a search
  engine being slow must never delay or fail a publish, and every submission
  attempt is then visible in Activity with its own status and error.
- The JOB RECORD is the submission history — no extra collection. Its payload
  carries the URLs and the source, `result` the endpoint receipt, `errorMessage`
  the reason a submission was rejected.
- Google is not a participant of IndexNow. There is deliberately no attempt to
  fake a Google push; the tab links to Search Console's URL Inspection instead.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.providers.base import ProviderError
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo

JOB_TYPE = "notify_search_engines"

# Sources: the two automatic triggers plus the manual page.
SOURCE_PUBLISH = "publish"
SOURCE_UPDATE = "update"
SOURCE_MANUAL = "manual"

DEFAULT_ENDPOINT = "https://api.indexnow.org/indexnow"


def publisher_host(pb: Any, project_id: str) -> str:
    """Best default host: the WordPress site the project publishes to."""
    row = IntegrationRepo(pb).get_active(project_id, "publisher")
    if not row:
        return ""
    base = str((row.get("configuration") or {}).get("base_url") or "").strip()
    return (urlparse(base).netloc or "").lower() if base else ""


def publisher_site_url(pb: Any, project_id: str) -> str:
    """The full WordPress site URL (with its subfolder), for display only."""
    row = IntegrationRepo(pb).get_active(project_id, "publisher")
    if not row:
        return ""
    return str((row.get("configuration") or {}).get("base_url") or "").strip().rstrip("/")


def get_config(pb: Any, project_id: str) -> dict[str, Any]:
    """The Index Now connection, normalized with defaults ({} when absent)."""
    row = IntegrationRepo(pb).first(filter=f'project="{project_id}" && category="indexing"')
    if not row:
        return {}
    cfg = dict(row.get("configuration") or {})
    enabled = bool(row.get("enabled", True))
    return {
        "id": row["id"],
        "enabled": enabled,
        "host": str(cfg.get("host") or ""),
        "key": str(cfg.get("key") or ""),
        "endpoint": str(cfg.get("endpoint") or DEFAULT_ENDPOINT),
        "key_location": str(cfg.get("key_location") or ""),
        "auto_publish": bool(cfg.get("auto_publish", True)),
        "auto_update": bool(cfg.get("auto_update", True)),
        "healthStatus": str(row.get("healthStatus") or "unknown"),
        "lastTestedAt": row.get("lastTestedAt") or "",
    }


def is_ready(config: dict[str, Any]) -> bool:
    """Key + host + enabled: without all three a submission is silently dropped."""
    return (
        bool(config)
        and bool(config.get("enabled"))
        and bool(config.get("key"))
        and bool(config.get("host"))
    )


async def install_key_file(publisher: Any, *, key: str) -> dict[str, Any]:
    """Upload ``<key>.txt`` to the WordPress media library.

    WordPress exposes no API for the site root, so the key goes in as media and
    is referenced through IndexNow's `keyLocation` parameter — which exists
    exactly for "the key file is not at the domain root". Returns
    ``{url, mediaId}``; the caller verifies the URL before trusting it (a host
    that blocks or rewrites .txt uploads would otherwise fail silently).
    """
    media = await publisher.upload_media(
        data=key.encode("utf-8"),
        filename=f"{key}.txt",
        title="IndexNow key",
        alt_text="IndexNow verification key",
    )
    url = str((media or {}).get("url") or "")
    if not url:
        raise ValueError("WordPress accepted the upload but returned no file URL")
    return {"url": url, "mediaId": int((media or {}).get("id") or 0)}


def should_notify(config: dict[str, Any], source: str) -> bool:
    if not is_ready(config):
        return False
    if source == SOURCE_MANUAL:
        return True
    if source == SOURCE_UPDATE:
        return bool(config.get("auto_update", True))
    return bool(config.get("auto_publish", True))


def queue(
    pb: Any,
    *,
    project: str,
    urls: list[str],
    source: str,
    article: str = "",
    force: bool = False,
) -> dict[str, Any] | None:
    """Queue one notification job; returns None when it is not wanted.

    Automatic runs dedupe per article+source+hour so a retried publish does not
    re-ping the engines; a manual request always runs (the user asked twice on
    purpose, and engines dedupe submissions themselves).
    """
    clean: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        if urlparse(url).scheme not in ("http", "https"):
            continue
        seen.add(url)
        clean.append(url)
    if not clean:
        return None

    if not force and not should_notify(get_config(pb, project), source):
        return None

    if force or source == SOURCE_MANUAL:
        idem = f"indexnow:{source}:{project}:{uuid.uuid4().hex[:12]}"
    else:
        hour = int(time.time() // 3600)
        idem = f"indexnow:{source}:{project}:{article or clean[0]}:{hour}"

    return JobRepo(pb).create(
        project=project,
        type=JOB_TYPE,
        payload={"urls": clean, "source": source, "articleId": article},
        idempotency_key=idem,
        max_attempts=3,
        entity_type="article" if article else "project",
        entity_id=article or project,
    )


@register_job(JOB_TYPE)
async def handle_notify_search_engines(ctx: JobContext) -> dict[str, Any]:
    """Push the published URLs to IndexNow. Raises only on a real failure."""
    payload = ctx.payload()
    urls = [str(u) for u in (payload.get("urls") or []) if str(u).strip()]
    source = str(payload.get("source") or SOURCE_MANUAL)
    if not urls:
        raise ValueError("notify_search_engines payload has no urls")

    integration = IntegrationRepo(ctx.pb).get_active(ctx.project_id, "indexing")
    if integration is None:
        # Configuration disappeared between queueing and running (or auto was
        # turned off): not an error, just nothing to do.
        ctx.info("no Index Now connection — nothing submitted", {"urls": len(urls)})
        return {"skipped": True, "reason": "no indexing connection", "urlCount": len(urls)}

    client = ctx.registry.get_indexing_provider(ctx.config.project, ctx.config.settings)
    if client is None:
        ctx.info("Index Now connection is disabled — nothing submitted", {"urls": len(urls)})
        return {"skipped": True, "reason": "connection disabled", "urlCount": len(urls)}

    ctx.stage_started("indexnow", f"Notifying search engines about {len(urls)} URL(s)…")
    ctx.progress(30, stage="indexnow", message="Sending to IndexNow…")
    try:
        result = await client.submit(urls)
    except ProviderError as exc:
        _mark_health(ctx, integration["id"], healthy=False)
        ctx.error_event(f"IndexNow submission failed: {exc}", {"urls": urls, **exc.details})
        raise
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            # Closing must never mask the submission result.
            with contextlib.suppress(Exception):
                await close()

    _mark_health(ctx, integration["id"], healthy=True)
    if result.note:
        ctx.warning(result.note, {"urls": urls, "keyLocation": result.note.split("=")[-1]})
    ctx.stage_completed(
        "indexnow",
        f"IndexNow accepted {result.url_count} URL(s)",
        {"statusCode": result.status_code, "host": result.host, "source": source},
    )
    ctx.progress(100, stage="done", message="Search engines notified")
    ctx.info(
        "indexnow submission accepted",
        {
            "statusCode": result.status_code,
            "host": result.host,
            "endpoint": result.endpoint,
            "urls": urls,
            "source": source,
        },
    )
    return {
        "accepted": result.accepted,
        "statusCode": result.status_code,
        "host": result.host,
        "endpoint": result.endpoint,
        "urlCount": result.url_count,
        "message": result.message,
        "source": source,
    }


def _mark_health(ctx: JobContext, integration_id: str, *, healthy: bool) -> None:
    from app.repositories.jobs import now_utc

    # Health bookkeeping is never fatal.
    with contextlib.suppress(Exception):
        IntegrationRepo(ctx.pb).mark_health(
            integration_id, "healthy" if healthy else "unhealthy", now_utc()
        )
