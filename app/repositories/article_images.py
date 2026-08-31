"""article_images repository — one row per generated image version.

Versioning contract: regenerate = new row (version+1); the selected image is
the row with active=true; older versions stay for compare/rollback. Successful
rows are never overwritten in place.
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

IMAGE_STATUSES = ("planned", "generating", "optimizing", "ready", "failed")


class ArticleImageRepo(BaseRepo):
    collection = "article_images"

    def create(
        self,
        *,
        project: str,
        article: str,
        role: str,
        version: int,
        section_key: str = "",
        provider: str = "",
        model: str = "",
        prompt: str = "",
        prompt_hash: str = "",
        negative_prompt: str = "",
        style_hash: str = "",
        width: int = 0,
        height: int = 0,
        aspect_ratio: str = "",
        fingerprint: str = "",
        alt_text: str = "",
        caption: str = "",
        filename: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        if role not in ("cover", "interior"):
            raise ValueError(f"invalid image role: {role}")
        return super().create(
            {
                "project": project,
                "article": article,
                "role": role,
                "sectionKey": section_key,
                "version": version,
                "active": False,
                "status": "generating",
                "attempts": 0,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "promptHash": prompt_hash,
                "negativePrompt": negative_prompt,
                "styleHash": style_hash,
                "width": width,
                "height": height,
                "aspectRatio": aspect_ratio,
                "fingerprint": fingerprint,
                "altText": alt_text,
                "caption": caption,
                "filename": filename,
                "error": {},
                "createdBy": created_by,
            }
        )

    def list_for_article(
        self, article_id: str, *, include_inactive: bool = True
    ) -> list[dict[str, Any]]:
        f = f'article="{article_id}"'
        if not include_inactive:
            f += " && active=true"
        return self.list_records(filter=f, sort="-version")

    def active_for_role(self, article_id: str, role: str) -> dict[str, Any] | None:
        return self.first(filter=f'article="{article_id}" && role="{role}" && active=true')

    def next_version(self, article_id: str, role: str, section_key: str = "") -> int:
        rows = self.list_records(
            filter=(f'article="{article_id}" && role="{role}" && sectionKey="{section_key}"'),
            sort="-version",
            per_page=1,
        )
        return int(rows[0].get("version") or 0) + 1 if rows else 1

    def by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        """A ready image with the same fingerprint can be reused verbatim."""
        if not fingerprint:
            return None
        return self.first(filter=f'fingerprint="{fingerprint}" && status="ready"')

    def for_version(
        self, article_id: str, role: str, section_key: str, version: int
    ) -> dict[str, Any] | None:
        """The row for an exact version slot (idempotent job re-runs reuse it)."""
        return self.first(
            filter=(
                f'article="{article_id}" && role="{role}" && sectionKey="{section_key}" '
                f"&& version={int(version)}"
            )
        )

    def store_optimized(
        self,
        image_id: str,
        *,
        optimized: tuple[str, bytes],
        small: tuple[str, bytes] | None = None,
        **meta: Any,
    ) -> dict[str, Any]:
        """Upload optimized binary(ies) to PocketBase files + metadata fields.

        Sync (SDK does blocking IO) — call from a worker thread context like
        every other repo call.
        """
        from pocketbase.models.file_upload import FileUpload

        body: dict[str, Any] = {"optimizedFile": FileUpload(optimized), **meta}
        if small is not None:
            body["smallFile"] = FileUpload(small)
        return self.update(image_id, body)

    def mark_optimizing(self, image_id: str) -> dict[str, Any]:
        return self.update(image_id, {"status": "optimizing"})

    def mark_ready(
        self,
        image_id: str,
        *,
        width: int = 0,
        height: int = 0,
        format: str = "",
        file_size: int = 0,
        filename: str = "",
        generation_latency: int = 0,
        estimated_cost: float | None = None,
        provider: str = "",
        model: str = "",
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "ready",
            "error": {},
            "generationLatency": generation_latency,
        }
        if width:
            body["width"] = width
        if height:
            body["height"] = height
        if format:
            body["format"] = format
        if file_size:
            body["fileSize"] = file_size
        if filename:
            body["filename"] = filename
        if estimated_cost is not None:
            body["estimatedCost"] = estimated_cost
        if provider:
            body["provider"] = provider
        if model:
            body["model"] = model
        return self.update(image_id, body)

    def mark_failed(self, image_id: str, error: dict[str, Any]) -> dict[str, Any]:
        return self.update(image_id, {"status": "failed", "error": error})

    def bump_attempts(self, image_id: str) -> int:
        row = self.get(image_id) or {}
        attempts = int(row.get("attempts") or 0) + 1
        self.update(image_id, {"attempts": attempts})
        return attempts

    def set_active(self, image_id: str) -> dict[str, Any]:
        """Select this version and deactivate its siblings (same role/slot)."""
        row = self.get(image_id)
        if not row:
            raise ValueError(f"image not found: {image_id}")
        siblings = self.list_records(
            filter=(
                f'article="{row["article"]}" && role="{row["role"]}" '
                f'&& sectionKey="{row.get("sectionKey") or ""}" && active=true'
            ),
        )
        for sibling in siblings:
            if sibling["id"] != image_id:
                self.update(sibling["id"], {"active": False})
        return self.update(image_id, {"active": True})

    def deactivate(self, image_id: str) -> dict[str, Any]:
        return self.update(image_id, {"active": False})

    def update_metadata(
        self,
        image_id: str,
        *,
        alt_text: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        body = {
            k: v
            for k, v in ({"altText": alt_text, "caption": caption, "filename": filename}).items()
            if v is not None
        }
        if not body:
            raise ValueError("nothing to update")
        return self.update(image_id, body)

    def set_wordpress_media(self, image_id: str, media_id: int, url: str) -> dict[str, Any]:
        return self.update(image_id, {"wordpressMediaId": media_id, "wordpressUrl": url})
