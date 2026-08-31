"""WordPress REST API adapter — full PublisherProvider protocol.

- Basic auth with application passwords (user:pass base64).
- create_post / update_post / get_post / list_posts / ping.
- Paginated, id-ordered listing (resumable via `after_id`).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.providers.base import PermanentError, PublishResult, WPPost
from app.providers.http import raise_for_provider, with_retry

PROVIDER_NAME = "wordpress"
DEFAULT_FIELDS = "id,title,link,status,modified,content"


class WordPressPublisher:
    category = "publisher"
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 60.0,
        attempts: int = 3,
        max_pages: int = 200,
        transport: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_pages = max_pages
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        if transport is not None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {token}",
                },
                timeout=httpx.Timeout(timeout, connect=15.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                transport=transport,
            )
        else:
            from app.providers.http import acquire_async_client

            self._client = acquire_async_client(
                self._base_url,
                auth_header=f"Basic {token}",
                timeout=timeout,
            )

    async def aclose(self) -> None:
        from app.providers.http import release_async_client

        release_async_client(self._client)

    # -- reads ----------------------------------------------------------------------
    async def list_posts(
        self,
        *,
        per_page: int = 100,
        after_id: int | None = None,
        status: str = "publish",
        fields: list[str] | None = None,
    ) -> list[WPPost]:
        """List posts ordered by id ascending; stops early once past `after_id`.

        Idempotent resume: with order=asc, a page starting with an id <= after_id
        means every following page is already processed — stop.
        """
        per_page = max(1, min(per_page, 100))
        params: dict[str, Any] = {
            "per_page": per_page,
            "orderby": "id",
            "order": "asc",
            "status": status,
            "_fields": ",".join(fields or [DEFAULT_FIELDS]),
        }
        posts: list[WPPost] = []
        for page in range(1, self._max_pages + 1):
            params["page"] = page

            async def _call() -> httpx.Response:
                return await self._client.get("/wp-json/wp/v2/posts", params=params)

            response = await with_retry(_call, attempts=3, what="wp.list_posts", logger_name="wp")
            if response.status_code == 400:
                # page beyond the end (WP returns 400 for out-of-range pages)
                break
            raise_for_provider(response, what="wp.list_posts")
            data = response.json()
            if not isinstance(data, list):
                raise PermanentError("wp.list_posts: unexpected response shape")

            for item in data:
                post_id = int(item.get("id") or 0)
                if post_id == 0:
                    continue
                if after_id is not None and post_id <= after_id:
                    return posts
                posts.append(
                    WPPost(
                        id=post_id,
                        title=_rendered(item.get("title")),
                        content_html=_rendered(item.get("content")),
                        link=str(item.get("link") or ""),
                        status=str(item.get("status") or status),
                        modified=str(item.get("modified") or ""),
                    )
                )
            if len(data) < per_page:
                break
        return posts

    async def get_post(self, post_id: int) -> WPPost | None:
        """Fetch a single post; None when it does not exist (404)."""

        async def _call() -> httpx.Response:
            return await self._client.get(f"/wp-json/wp/v2/posts/{post_id}")

        response = await with_retry(_call, attempts=2, what="wp.get_post", logger_name="wp")
        if response.status_code == 404:
            return None
        raise_for_provider(response, what="wp.get_post")
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise PermanentError("wp.get_post: unexpected response shape")
        return WPPost(
            id=int(data["id"]),
            title=_rendered(data.get("title")),
            content_html=_rendered(data.get("content")),
            link=str(data.get("link") or ""),
            status=str(data.get("status") or ""),
            modified=str(data.get("modified") or ""),
        )

    # -- writes ---------------------------------------------------------------------
    async def create_post(
        self,
        *,
        title: str,
        html: str,
        status: str,
        slug: str,
        meta: dict | None = None,
        excerpt: str = "",
    ) -> PublishResult:
        _validate_status(status)
        body: dict[str, Any] = {
            "title": title,
            "content": html,
            "status": status,
            "slug": slug[:200],
        }
        if excerpt:
            body["excerpt"] = excerpt
        if meta:
            body["meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}

        async def _call() -> httpx.Response:
            return await self._client.post("/wp-json/wp/v2/posts", json=body)

        response = await with_retry(_call, attempts=3, what="wp.create_post", logger_name="wp")
        raise_for_provider(response, what="wp.create_post")
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise PermanentError("wp.create_post: response missing post id")
        return PublishResult(
            post_id=int(data["id"]),
            link=str(data.get("link") or ""),
            status_code=response.status_code,
        )

    async def update_post(
        self,
        post_id: int,
        *,
        title: str | None = None,
        html: str | None = None,
        status: str | None = None,
        slug: str | None = None,
        meta: dict | None = None,
        excerpt: str | None = None,
    ) -> PublishResult:
        if status is not None:
            _validate_status(status)
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if html is not None:
            body["content"] = html
        if status is not None:
            body["status"] = status
        if slug is not None:
            body["slug"] = slug[:200]
        if excerpt is not None:
            body["excerpt"] = excerpt
        if meta:
            body["meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
        if not body:
            raise PermanentError("wp.update_post: nothing to update")

        async def _call() -> httpx.Response:
            return await self._client.post(f"/wp-json/wp/v2/posts/{post_id}", json=body)

        response = await with_retry(_call, attempts=3, what="wp.update_post", logger_name="wp")
        raise_for_provider(response, what="wp.update_post")
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise PermanentError("wp.update_post: response missing post id")
        return PublishResult(
            post_id=int(data["id"]),
            link=str(data.get("link") or ""),
            status_code=response.status_code,
        )

    async def ping(self) -> None:
        """Health probe — GET the WP REST index. Raises ProviderError on failure."""

        async def _call() -> httpx.Response:
            return await self._client.get("/wp-json")

        response = await with_retry(_call, attempts=2, what="wp.ping", logger_name="wp")
        raise_for_provider(response, what="wp.ping")

    async def find_post_by_slug(self, slug: str) -> WPPost | None:
        """Find a post by its exact slug (WP REST `slug` param — reliable).

        Used to recover from a crash between `create_post` and storing the
        post id: a retried publish finds the orphaned post by the article
        slug and UPDATEs it instead of creating a duplicate.

        NOTE: meta-based lookups (meta_key/meta_value) are NOT used here —
        WP REST silently ignores those params for unregistered meta, which
        made `find_post_by_meta` return the FIRST post for any query.
        """
        if not slug:
            return None
        params: dict[str, Any] = {
            "per_page": 5,
            "_fields": "id,title,link,status,modified,content",
            "slug": slug,
        }

        async def _call() -> httpx.Response:
            return await self._client.get("/wp-json/wp/v2/posts", params=params)

        response = await with_retry(
            _call, attempts=2, what="wp.find_post_by_slug", logger_name="wp"
        )
        raise_for_provider(response, what="wp.find_post_by_slug")
        data = response.json()
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        return WPPost(
            id=int(item.get("id") or 0),
            title=_rendered(item.get("title")),
            content_html=_rendered(item.get("content")),
            link=str(item.get("link") or ""),
            status=str(item.get("status") or ""),
            modified=str(item.get("modified") or ""),
        )

    async def unpublish_post(self, post_id: int) -> PublishResult:
        """Safely unpublish: WP has no true unpublish, so set status=private
        (reversible via update_post)."""
        return await self.update_post(post_id, status="private")

    # -- media -----------------------------------------------------------------------
    async def upload_media(
        self,
        *,
        data: bytes,
        filename: str,
        title: str = "",
        alt_text: str = "",
        caption: str = "",
        post_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /wp-json/wp/v2/media (multipart). Metadata fields localize the
        attachment (alt/title/caption). Caller dedupes via stored media id."""
        files = {"file": (filename, data, _guess_mime(filename))}
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if alt_text:
            fields["alt_text"] = alt_text
        if caption:
            fields["caption"] = caption
        if post_id:
            fields["post"] = str(post_id)

        async def _call() -> httpx.Response:
            return await self._client.post("/wp-json/wp/v2/media", files=files, data=fields)

        response = await with_retry(_call, attempts=3, what="wp.upload_media", logger_name="wp")
        raise_for_provider(response, what="wp.upload_media")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise PermanentError("wp.upload_media: response missing media id")
        return {
            "id": int(payload["id"]),
            "url": str(payload.get("source_url") or ""),
            "mime_type": str(payload.get("mime_type") or ""),
        }

    async def set_featured_media(self, post_id: int, media_id: int) -> PublishResult:
        async def _call() -> httpx.Response:
            return await self._client.post(
                f"/wp-json/wp/v2/posts/{post_id}", json={"featured_media": int(media_id)}
            )

        response = await with_retry(
            _call, attempts=3, what="wp.set_featured_media", logger_name="wp"
        )
        raise_for_provider(response, what="wp.set_featured_media")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise PermanentError("wp.set_featured_media: response missing post id")
        return PublishResult(
            post_id=int(payload["id"]),
            link=str(payload.get("link") or ""),
            status_code=response.status_code,
        )

    async def _list_taxonomy(
        self, endpoint: str, per_page: int = 100, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        """Paginated taxonomy fetch (categories/tags)."""
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "per_page": per_page,
                "page": page,
                "_fields": "id,name,slug,count",
            }

            async def _call(page: int = page, params: dict[str, Any] = params) -> httpx.Response:
                return await self._client.get(f"/wp-json/wp/v2/{endpoint}", params=params)

            response = await with_retry(_call, attempts=2, what=f"wp.{endpoint}", logger_name="wp")
            if response.status_code == 400:
                break
            raise_for_provider(response, what=f"wp.{endpoint}")
            data = response.json()
            if not isinstance(data, list):
                break
            items.extend(item for item in data if isinstance(item, dict))
            if len(data) < per_page:
                break
        return items

    async def list_categories(self) -> list[dict[str, Any]]:
        return await self._list_taxonomy("categories")

    async def list_tags(self) -> list[dict[str, Any]]:
        return await self._list_taxonomy("tags")


def _validate_status(status: str) -> None:
    if status not in ("draft", "publish", "pending", "private"):
        raise PermanentError(f"wp: invalid status {status!r}")


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "webp": "image/webp",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "avif": "image/avif",
        "gif": "image/gif",
    }.get(ext, "application/octet-stream")


def _rendered(field: object) -> str:
    if isinstance(field, dict):
        return str(field.get("rendered") or field.get("raw") or "")
    return str(field or "")
