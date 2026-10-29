"""Index Now — the manual submission page and its configuration routes.

Google is not on IndexNow, so the page is explicit about what each engine gets:
IndexNow (Bing, Yandex, Naver, Seznam, Yep) is pushed from here, and Google gets
a Search Console "Request indexing" link per URL — the only per-URL path Google
offers for ordinary content.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, Response

from app.api.deps import (
    require_hx,
    require_project_access,
    require_project_role,
    safe_bool,
    safe_str,
)
from app.api.errors import hx_error
from app.domain.validation import normalize_base_url
from app.providers.indexnow import (
    DEFAULT_ENDPOINT,
    generate_key,
    gsc_inspect_link,
    url_host,
)
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from app.services import search_engines
from app.services.secrets import get_secrets_service
from app.utils import error_response, toast_response

router = APIRouter()

PROVIDER = "indexnow"
DISPLAY_NAME = "Index Now"


def load_config(pb: Any, project_id: str) -> dict[str, Any]:
    return search_engines.get_config(pb, project_id)


def _heal_category(pb: Any) -> None:
    """Union the code's select values into the live PocketBase (idempotent).

    A category the collection does not list yet is rejected with
    "category: Invalid value: indexing." — `make bootstrap` fixes that, but a
    deployment that upgraded the code without re-running it would otherwise
    never be able to save this connection. `ensure_select_values` is the same
    reconciler bootstrap uses and is a no-op once the schema matches.
    """
    from app.scripts.bootstrap_pb import ensure_select_values

    ensure_select_values(pb)


def _is_invalid_value(exc: Exception) -> bool:
    data = getattr(exc, "data", None)
    text = f"{exc} {data if isinstance(data, dict) else ''}".lower()
    return "invalid value" in text or "validation_invalid_value" in text


def _save(
    pb: Any,
    project_id: str,
    *,
    host: str,
    key: str,
    endpoint: str,
    key_location: str,
    auto_publish: bool,
    auto_update: bool,
    gsc_property: str = "",
    enabled: bool = True,
    user_id: str = "",
) -> dict[str, Any]:
    repo = IntegrationRepo(pb)
    row = repo.first(filter=f'project="{project_id}" && category="indexing"')
    configuration = {
        "host": host,
        "key": key,
        "endpoint": endpoint or DEFAULT_ENDPOINT,
        "key_location": key_location,
        "auto_publish": auto_publish,
        "auto_update": auto_update,
        "gsc_property": gsc_property,
    }
    if row:
        return repo.update(row["id"], {"configuration": configuration, "enabled": enabled})
    try:
        return repo.create(
            project=project_id,
            category="indexing",
            provider=PROVIDER,
            display_name=DISPLAY_NAME,
            configuration=configuration,
            secrets_enc="",
            enabled=enabled,
            created_by=user_id,
        )
    except Exception as exc:
        if not _is_invalid_value(exc):
            raise
        # Older deployment whose `integrations.category` select predates this
        # feature: heal the schema once and retry (see _heal_category).
        _heal_category(pb)
        return repo.create(
            project=project_id,
            category="indexing",
            provider=PROVIDER,
            display_name=DISPLAY_NAME,
            configuration=configuration,
            secrets_enc="",
            enabled=enabled,
            created_by=user_id,
        )


def _persist(pb: Any, project_id: str, config: dict[str, Any], **overrides: Any) -> None:
    """Save the connection from an existing (normalized) config plus overrides."""
    fields: dict[str, Any] = {
        "host": str(config.get("host") or ""),
        "key": str(config.get("key") or ""),
        "endpoint": str(config.get("endpoint") or DEFAULT_ENDPOINT),
        "key_location": str(config.get("key_location") or ""),
        "auto_publish": bool(config.get("auto_publish", True)),
        "auto_update": bool(config.get("auto_update", True)),
        "gsc_property": str(config.get("gsc_property") or ""),
        "enabled": True,
    }
    fields.update(overrides)
    _save(pb, project_id, **fields)


def _host_from_form(pb: Any, project_id: str, host: str) -> str:
    """Normalize the host; default to the WordPress site when left empty."""
    raw = safe_str(host).lower()
    if raw:
        from urllib.parse import urlparse

        # Accept "example.com", "https://example.com/" and "example.com/blog" —
        # the host is what the key file and every URL are matched against.
        return (urlparse(normalize_base_url(raw)).netloc or raw).strip("/")
    return search_engines.publisher_host(pb, project_id)


def _refresh(message: str, *, type: str = "success") -> Response:
    return toast_response(
        message, type=type, extra_events={"refreshIndexNow": True, "refreshJobs": True}
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/index-now/config")
@hx_error("Saving the Index Now settings failed")
def save_config(
    request: Request,
    project_id: str,
    host: str = Form(""),
    endpoint: str = Form(""),
    key_location: str = Form(""),
    auto_publish: str = Form(""),
    auto_update: str = Form(""),
    gsc_property: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    existing = load_config(request.state.pb, project_id)
    resolved_host = _host_from_form(request.state.pb, project_id, host)
    if not resolved_host:
        return error_response(
            "Enter the website host (for example example.com) — the key file is "
            "looked up there, so it cannot be guessed."
        )
    key = str(existing.get("key") or "") or generate_key()
    resolved_location = safe_str(key_location)
    _save(
        request.state.pb,
        project_id,
        host=resolved_host,
        key=key,
        endpoint=safe_str(endpoint) or DEFAULT_ENDPOINT,
        key_location=resolved_location,
        auto_publish=safe_bool(auto_publish),
        auto_update=safe_bool(auto_update),
        gsc_property=safe_str(gsc_property),
        enabled=True,
        user_id=request.state.user.get("id", "") if request.state.user else "",
    )
    if not existing.get("key"):
        return _refresh("Key generated — put the file on your site, then press Verify")
    return _refresh("Index Now settings saved")


@router.post("/projects/{project_id}/index-now/key")
@hx_error("Generating a key failed")
def regenerate_key(request: Request, project_id: str):
    """New key. Invalidates the old file — the page says so before it happens."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    existing = load_config(request.state.pb, project_id)
    if not existing:
        return error_response("Save the website host first, then generate the key")
    _persist(request.state.pb, project_id, existing, key=generate_key(), key_location="")
    return _refresh("New key generated — replace the file on your site, then verify")


@router.post("/projects/{project_id}/index-now/verify")
@hx_error("Checking the key file failed")
async def verify_key(request: Request, project_id: str):
    """Fetch <host>/<key>.txt and confirm it serves this key.

    Without a reachable key file every submission is accepted (202) and then
    dropped, so this check is the one that decides whether submissions work.
    """
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    integration = IntegrationRepo(request.state.pb).get_active(project_id, "indexing")
    if integration is None:
        return error_response("Save the Index Now settings first")
    registry = _registry(request)
    client = registry.get_indexing_provider({"id": project_id}, {}, integration=integration)
    if client is None:
        return error_response("Index Now is not enabled for this website")

    # Check BOTH places an engine can look: the domain root (which every engine
    # supports) and the configured location. The report is what the engines see.
    checks = []
    try:
        root = await client.check_key(client.root_key_url)
        checks.append(root)
        if client.key_location and client.key_location != client.root_key_url:
            checks.append(await client.check_key(client.key_location))
        elif not client.key_location and client.key_url != client.root_key_url:
            checks.append(await client.check_key(client.key_url))
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()

    ok = [c for c in checks if c.ok]
    if not ok:
        detail = " · ".join(f"{c.url} → {c.detail}" for c in checks)
        return toast_response(
            f"No key file is reachable: {detail}",
            type="error",
            extra_events={"refreshIndexNow": True},
        )

    from app.repositories.jobs import now_utc

    IntegrationRepo(request.state.pb).mark_health(integration["id"], "healthy", now_utc())
    used = ok[0]
    others = [c for c in checks if not c.ok]
    message = f"Key file verified at {used.url}"
    if used is not root:
        message += f" — the domain root {root.url} does not serve it ({root.detail})"
    for other in others:
        if other is not root:
            message += f" · {other.url} {other.detail}"
    return toast_response(message, type="success", extra_events={"refreshIndexNow": True})


@router.post("/projects/{project_id}/index-now/submit")
@hx_error("Submitting the URLs failed")
def submit_urls(request: Request, project_id: str, urls: str = Form("")):
    """Manual submission: one URL per line (or comma/space separated)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    config = load_config(request.state.pb, project_id)
    if not search_engines.is_ready(config):
        return error_response("Save the website host and key before submitting")

    raw = safe_str(urls)
    if not raw:
        return error_response("Paste at least one URL to submit")
    parsed = [part.strip() for part in raw.replace(",", "\n").replace(" ", "\n").splitlines()]
    host = str(config.get("host") or "")
    from urllib.parse import urlparse

    accepted: list[str] = []
    rejected: list[str] = []
    for url in parsed:
        if not url:
            continue
        if "://" not in url:
            url = f"https://{url}"
        if urlparse(url).netloc.lower() != host:
            rejected.append(url)
        else:
            accepted.append(url)
    if not accepted:
        return error_response(
            f"None of those URLs is on {host} — IndexNow only accepts pages of the "
            "configured website."
        )
    job = search_engines.queue(
        request.state.pb,
        project=project_id,
        urls=accepted,
        source=search_engines.SOURCE_MANUAL,
        force=True,
    )
    message = f"Submitting {len(accepted)} URL(s) to IndexNow"
    if rejected:
        message += f" — {len(rejected)} skipped (not on {host})"
    if job is None:  # pragma: no cover - queue always returns a job for manual runs
        return error_response("Could not queue the submission")
    return _refresh(message)


def _registry(request: Request) -> Any:
    from app.providers.registry import ProviderRegistry

    return ProviderRegistry(request.state.pb, secrets=get_secrets_service())


# ---------------------------------------------------------------------------
# Key file download
# ---------------------------------------------------------------------------
@router.get("/projects/{project_id}/index-now/key.txt")
def download_key_file(request: Request, project_id: str):
    """The exact bytes to upload to the site root — engines compare it exactly."""
    require_project_access(request, project_id)
    config = load_config(request.state.pb, project_id)
    key = str(config.get("key") or "")
    if not key:
        return PlainTextResponse("No key generated yet.\n", status_code=404)
    return PlainTextResponse(
        key,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{key}.txt"'},
    )


# ---------------------------------------------------------------------------
# Page helpers (used by the project tab)
# ---------------------------------------------------------------------------
def tab_context(pb: Any, project_id: str) -> dict[str, Any]:
    """Everything the Index Now tab renders: config, key file, submissions."""
    config = load_config(pb, project_id)
    jobs = JobRepo(pb).list_records(
        filter=f'project="{project_id}" && type="{search_engines.JOB_TYPE}"',
        sort="-created",
        per_page=25,
    )
    key = str(config.get("key") or "")
    host = str(config.get("host") or "")
    key_host = url_host(str(config.get("key_location") or ""))
    key_url = ""
    if key:
        from app.providers.indexnow import IndexNowClient

        key_url = IndexNowClient(
            host=host, key=key, key_location=str(config.get("key_location") or "")
        ).key_url
    published = _published_urls(pb, project_id)
    return {
        "index_now": config,
        "index_now_ready": search_engines.is_ready(config),
        "index_now_key": key,
        "index_now_key_url": key_url,
        # The address the file must exist at, unless a key file URL is set.
        "index_now_root_url": (f"https://{host}/{key}.txt" if host and key else ""),
        # The engine verifies the key on the host it receives: a file served from
        # another host (www vs bare, a CDN) is rejected — surface it up front.
        "index_now_key_host": key_host,
        "index_now_mismatch": bool(key_host) and key_host != host,
        "index_now_default_host": search_engines.publisher_host(pb, project_id),
        "index_now_site_url": search_engines.publisher_site_url(pb, project_id),
        "index_now_default_endpoint": DEFAULT_ENDPOINT,
        "index_now_jobs": jobs,
        "index_now_published": published,
        "index_now_gsc": [
            {
                "url": row["url"],
                "link": gsc_inspect_link(row["url"], f"https://{host}" if host else ""),
            }
            for row in published[:25]
        ],
    }


def _published_urls(pb: Any, project_id: str) -> list[dict[str, Any]]:
    from app.repositories.articles import ArticleRepo

    rows = ArticleRepo(pb).list_records(
        filter=f'project="{project_id}" && status="published" && wordpressUrl != ""',
        sort="-updated",
        per_page=25,
        fields="id,title,wordpressUrl,updated",
    )
    return [
        {"id": r.get("id", ""), "title": r.get("title") or "", "url": r.get("wordpressUrl") or ""}
        for r in rows
        if r.get("wordpressUrl")
    ]
