"""Image generation / optimization / WP-upload job handlers.

Handlers registered here:
- generate_cover_image / generate_interior_image / generate_article_image
  (generation → deterministic quality gate → optional AI QA → optimization →
  PocketBase file storage)
- optimize_article_image (re-run optimization for an existing source file)
- publish_article_image (idempotent WordPress media upload for one image)

`apply_images_to_html` is the publish-time integration used by
publishing_service: validates, uploads media (reusing stored WP media ids),
inserts figure/figcaption at {{IMAGE:section-N}} placeholders and returns the
featured-image media id.

AI visual QA (config-gated via imageAiQaEnabled) uses an OPTIONAL
`generate_vision_json(system, user, image)` method on the review LLM — only
the Gemini adapter implements it today; providers without it skip QA with a
warning event (never blocks generation).

# ponytail: image provider concurrency is bounded by the worker's
# max_concurrent_jobs semaphore; a dedicated image semaphore is only worth
# adding when image jobs start starving other job types.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from typing import Any

from app.config import settings as app_settings
from app.domain.images import (
    COVER_KEY,
    MAX_SOURCE_BYTES,
    MIN_SOURCE_BYTES,
    ArticleImagePlan,
    classify_error,
    estimate_cost,
    figure_html,
    image_filename,
    image_fingerprint,
    resolve_image_placeholders,
    style_fingerprint_source,
    validate_publishable_images,
)
from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.providers.base import ImageRequest, PermanentError, ProviderError, TransientError
from app.repositories.article_images import ArticleImageRepo
from app.repositories.articles import ArticleRepo

_FILE_URL = "{base}/api/files/article_images/{record}/{filename}"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _role_provider_config(imgs: dict[str, Any], role: str) -> dict[str, str]:
    if role == "cover":
        return {"provider": imgs["cover_provider"], "model": imgs["cover_model"]}
    return {"provider": imgs["interior_provider"], "model": imgs["interior_model"]}


def _fallback_provider_config(imgs: dict[str, Any], role: str) -> dict[str, str]:
    return {"provider": imgs["fallback_provider"] or "", "model": imgs["fallback_model"] or ""}


def _build_image_provider(ctx: JobContext, cfg: dict[str, str], cache: dict[str, Any]) -> Any:
    key = f"{cfg['provider']}:{cfg['model']}"
    if key not in cache:
        cache[key] = ctx.registry.get_image_provider(
            ctx.config.project,
            ctx.config.settings,
            ctx.providers.observer,
            role_config=cfg,
        )
    return cache[key]


def _style_hash(imgs: dict[str, Any]) -> str:
    return hashlib.sha256(style_fingerprint_source(imgs["style"]).encode()).hexdigest()[:32]


def pb_file_url(record_id: str, filename: str) -> str:
    """Public PocketBase file URL (file fields are unprotected by design)."""
    return _FILE_URL.format(
        base=app_settings.pb_url.rstrip("/"), record=record_id, filename=filename
    )


async def _download_file(url: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    """Download a PB-hosted image. The PB host is trusted platform infra
    (same origin the SDK already talks to) — no SSRF re-validation here."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            raise TransientError(f"file download failed: HTTP {response.status_code}")
        data = response.content
    if len(data) > max_bytes:
        raise PermanentError(f"file exceeds size limit ({len(data)} bytes)")
    return data


# ---------------------------------------------------------------------------
# deterministic quality gate
# ---------------------------------------------------------------------------
def _quality_gate(
    data: bytes,
    mime_type: str,
    *,
    role: str,
    min_width: int,
    expected: tuple[int, int],
) -> dict[str, Any]:
    """Decode + validate BEFORE anything is stored. Raises PermanentError."""
    from PIL import Image

    if len(data) < MIN_SOURCE_BYTES:
        raise PermanentError(
            f"image too small ({len(data)} bytes)", details={"category": "decode_failure"}
        )
    if len(data) > MAX_SOURCE_BYTES:
        raise PermanentError(
            f"image too large ({len(data)} bytes)", details={"category": "decode_failure"}
        )
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            fmt = (img.format or "").lower()
    except Exception as exc:
        raise PermanentError(
            f"image cannot be decoded: {exc}", details={"category": "decode_failure"}
        ) from exc
    if fmt not in ("png", "jpeg", "webp"):
        raise PermanentError(
            f"unsupported source format: {fmt}", details={"category": "invalid_response"}
        )
    if role == "cover" and min_width and width < min_width:
        raise PermanentError(
            f"cover width {width}px is below the required {min_width}px",
            details={"category": "invalid_response"},
        )
    if expected[0] and expected[1]:
        target_ar = expected[0] / expected[1]
        actual_ar = width / max(height, 1)
        if abs(target_ar - actual_ar) / target_ar > 0.08:
            # aspect drifted: warn but keep (model delivered what it delivered)
            return {"width": width, "height": height, "format": fmt, "aspect_drift": True}
    return {"width": width, "height": height, "format": fmt}


# ---------------------------------------------------------------------------
# optional AI visual QA (config-gated)
# ---------------------------------------------------------------------------
_QA_SYSTEM = (
    "You are an image QA reviewer. Answer ONLY with JSON: "
    '{"ok": true/false, "reason": "short reason"}. '
    "Reject images that contain rendered text/letters/watermarks, are broken, "
    "or clearly do not match the requested subject."
)


async def _ai_qa(ctx: JobContext, data: bytes, mime_type: str, subject: str) -> None:
    llm = ctx.providers.llm_for("review")
    vision = getattr(llm, "generate_vision_json", None)
    if vision is None:
        ctx.warning("ai visual QA skipped: review model has no vision support")
        return
    verdict = await vision(
        system=_QA_SYSTEM,
        user=f"Requested subject: {subject[:500]}",
        image=(data, mime_type),
    )
    if not verdict.get("ok", False):
        raise PermanentError(
            f"AI visual QA rejected the image: {verdict.get('reason') or 'unspecified'}",
            details={"category": "content_policy"},
        )


# ---------------------------------------------------------------------------
# optimization
# ---------------------------------------------------------------------------
class ImageOptimizationService:
    """decode → validate → strip metadata → downscale → re-encode.

    No upscaling: sources larger than max_width are downscaled, smaller ones
    kept as-is. AVIF falls back to webp when the Pillow codec is missing.
    """

    def __init__(self, *, max_width: int = 1600, small_width: int = 640, quality: int = 85) -> None:
        self.max_width = max_width
        self.small_width = small_width
        self.quality = quality

    @staticmethod
    def _normalize(img: Any) -> Any:
        # metadata never survives: we re-encode from raw pixels (no exif copy)
        if img.mode not in ("RGB", "RGBA"):
            return img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")
        return img

    def _encode(self, img: Any, fmt: str) -> bytes:
        buf = io.BytesIO()
        params: dict[str, Any] = {"format": fmt.upper()}
        if fmt in ("webp", "jpeg"):
            params["quality"] = self.quality
        if fmt == "jpeg" and img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(buf, **params)
        return buf.getvalue()

    def _target_format(self, requested: str) -> str:
        fmt = (requested or "webp").lower()
        if fmt == "avif":
            try:
                from PIL import features

                if not features.check("avif"):
                    return "webp"
            except Exception:
                return "webp"
        return fmt

    def optimize(
        self, data: bytes, *, target_format: str = "webp"
    ) -> tuple[bytes, str, int, int, bytes | None]:
        """→ (optimized_bytes, mime, width, height, small_variant|None)."""
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as source:
                img = self._normalize(source)
                if img.width > self.max_width:
                    ratio = self.max_width / img.width
                    img = img.resize(
                        (self.max_width, round(img.height * ratio)), Image.Resampling.LANCZOS
                    )
                fmt = self._target_format(target_format)
                optimized = self._encode(img, fmt)
                width, height = img.size

                small: bytes | None = None
                if img.width > self.small_width:
                    ratio = self.small_width / img.width
                    small_img = img.resize(
                        (self.small_width, round(img.height * ratio)), Image.Resampling.LANCZOS
                    )
                    small = self._encode(small_img, fmt)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise PermanentError(
                f"image optimization failed: {exc}",
                details={"category": "optimization_failure"},
            ) from exc

        mime = f"image/{fmt}"
        return optimized, mime, width, height, small


# ---------------------------------------------------------------------------
# generation job (shared by all three generate_* types)
# ---------------------------------------------------------------------------
async def _generate_image(ctx: JobContext, *, role: str, section_key: str) -> dict[str, Any]:
    payload = ctx.payload()
    article_id = (payload.get("articleId") or "").strip()
    if not article_id:
        raise ValueError("image job payload is missing articleId")
    if role != "cover":
        section_key = (payload.get("sectionKey") or section_key or "").strip()
        if not section_key:
            raise ValueError("interior image payload is missing sectionKey")

    articles = ArticleRepo(ctx.pb)
    article = articles.get(article_id)
    if not article:
        raise ValueError(f"article not found: {article_id}")

    plan_raw = article.get("imagePlan")
    if not plan_raw:
        raise PermanentError(
            "برنامه تصاویر ساخته نشده — ابتدا «برنامه تصاویر» را تولید کنید",
            details={"article": article_id},
        )
    plan = ArticleImagePlan.model_validate(plan_raw)
    spec = plan.find(role, section_key)
    if spec is None:
        raise PermanentError(
            f"no {role} image planned for {section_key or 'cover'}",
            details={"article": article_id},
        )

    imgs = ctx.config.images
    provider_cfg = _role_provider_config(imgs, role)
    fallback_cfg = _fallback_provider_config(imgs, role)
    width, height = (spec.suggested_size or [0, 0])[:2] or (0, 0)
    if not (width and height):
        from app.domain.images import dims_for_aspect

        width, height = dims_for_aspect(
            spec.aspect_ratio or "16:9", role=role, min_width=imgs["cover_min_width"]
        )

    fingerprint = image_fingerprint(
        article_id=article_id,
        role=role,
        section_key=section_key,
        plan_version=plan.version,
        provider=provider_cfg["provider"],
        model=provider_cfg["model"],
        prompt=spec.prompt,
        style_config=imgs["style"],
        width=width,
        height=height,
    )

    repo = ArticleImageRepo(ctx.pb)
    # idempotency: identical config already generated → reuse, never re-bill.
    # Explicit regeneration (payload.version set) always creates a new version.
    reusable = None if payload.get("version") else repo.by_fingerprint(fingerprint)
    if reusable is not None:
        repo.set_active(reusable["id"])
        ctx.info(
            "image reused from identical fingerprint",
            {"imageId": reusable["id"], "role": role, "sectionKey": section_key},
        )
        return {"articleId": article_id, "imageId": reusable["id"], "reused": True}

    version = int(payload.get("version") or 0) or repo.next_version(article_id, role, section_key)
    row = repo.for_version(article_id, role, section_key, version)
    if row is None:
        row = repo.create(
            project=ctx.project_id,
            article=article_id,
            role=role,
            version=version,
            section_key=section_key,
            provider=provider_cfg["provider"],
            model=provider_cfg["model"],
            prompt=spec.prompt,
            prompt_hash=hashlib.sha256(spec.prompt.encode()).hexdigest()[:32],
            negative_prompt=spec.negative_prompt,
            style_hash=_style_hash(imgs),
            width=width,
            height=height,
            aspect_ratio=spec.aspect_ratio,
            fingerprint=fingerprint,
            alt_text=spec.alt_text,
            caption=spec.caption,
            filename=image_filename(
                article_title=article.get("title") or "",
                article_id=article_id,
                role=role,
                section_key=section_key,
                ext="webp",
                image_id="pending",
            ),
        )
    else:
        # idempotent re-run of the same job (crash recovery)
        repo.update(
            row["id"],
            {
                "status": "generating",
                "error": {},
                "prompt": spec.prompt,
                "fingerprint": fingerprint,
                "styleHash": _style_hash(imgs),
            },
        )
    image_id = row["id"]
    attempts = repo.bump_attempts(image_id)

    ctx.stage_started("generating", "در حال تولید تصویر…")
    ctx.progress(10, stage="generating", message="در حال تولید تصویر…")
    await ctx.check_cancelled()

    cache: dict[str, Any] = {}
    started = time.monotonic()
    used_cfg = provider_cfg
    request = ImageRequest(
        prompt=spec.prompt,
        negative_prompt=spec.negative_prompt,
        width=width,
        height=height,
        aspect_ratio=spec.aspect_ratio,
        output_format="png",
        seed=payload.get("seed") or None,
    )
    try:
        provider = _build_image_provider(ctx, provider_cfg, cache)
        try:
            result = await provider.generate_image(request)
        except ProviderError:
            if not fallback_cfg["provider"] or fallback_cfg["provider"] == provider_cfg["provider"]:
                raise
            ctx.warning(
                f"primary image provider failed — trying fallback {fallback_cfg['provider']}",
                {"role": role, "sectionKey": section_key},
            )
            used_cfg = fallback_cfg
            fallback = _build_image_provider(ctx, fallback_cfg, cache)
            result = await fallback.generate_image(request)
    except ProviderError as exc:
        repo.mark_failed(
            image_id,
            {
                "category": (
                    (getattr(exc, "details", {}) or {}).get("category")
                    or classify_error(str(exc), getattr(exc, "retryable", False))
                ),
                "message": str(exc)[:1000],
                "retryable": getattr(exc, "retryable", False),
                "attempts": attempts,
            },
        )
        raise

    # deterministic quality gate + optional AI QA + optimization + storage —
    # a failure in any of these marks the row failed (job still surfaces it)
    try:
        gate = _quality_gate(
            result.data,
            result.mime_type,
            role=role,
            min_width=imgs["cover_min_width"] if role == "cover" else 0,
            expected=(width, height),
        )
        if gate.get("aspect_drift"):
            ctx.warning("generated image aspect drifted from plan", {"imageId": image_id})

        if imgs["ai_qa_enabled"]:
            ctx.progress(45, stage="qa", message="بررسی کیفیت تصویر…")
            await _ai_qa(ctx, result.data, result.mime_type, spec.prompt)

        # optimize + store
        ctx.stage_started("optimizing", "در حال بهینه‌سازی تصویر…")
        ctx.progress(60, stage="optimizing", message="در حال بهینه‌سازی تصویر…")
        repo.mark_optimizing(image_id)
        service = ImageOptimizationService()
        optimized, mime, opt_w, opt_h, small = service.optimize(
            result.data, target_format=imgs["optimization_format"]
        )
    except ProviderError as exc:
        repo.mark_failed(
            image_id,
            {
                "category": (
                    (getattr(exc, "details", {}) or {}).get("category")
                    or classify_error(str(exc), getattr(exc, "retryable", False))
                ),
                "message": str(exc)[:1000],
                "retryable": getattr(exc, "retryable", False),
                "attempts": attempts,
            },
        )
        raise
    ext = mime.split("/", 1)[1]
    filename = image_filename(
        article_title=article.get("title") or "",
        article_id=article_id,
        role=role,
        section_key=section_key,
        ext=ext,
        image_id=image_id,
    )
    small_name = filename.replace(f".{ext}", f"-640.{ext}")
    from pocketbase.models.file_upload import FileUpload

    files: dict[str, Any] = {
        "sourceFile": FileUpload((f"source-{image_id}.png", result.data)),
        "optimizedFile": FileUpload((filename, optimized)),
    }
    if small is not None:
        files["smallFile"] = FileUpload((small_name, small))
    ctx.pb.collection("article_images").update(image_id, files)

    cost = estimate_cost(used_cfg["provider"], opt_w, opt_h, result.usage)
    repo.mark_ready(
        image_id,
        width=opt_w,
        height=opt_h,
        format=mime,
        file_size=len(optimized),
        filename=filename,
        generation_latency=result.latency_ms or int((time.monotonic() - started) * 1000),
        estimated_cost=cost,
        provider=used_cfg["provider"],
        model=used_cfg["model"],
    )
    repo.set_active(image_id)

    duration_ms = int((time.monotonic() - started) * 1000)
    ctx.stage_completed("optimizing", "تصویر آماده شد")
    ctx.progress(100, stage="done", message="تصویر آماده شد")
    # usage accounting: image_generated (no prompt content — internal event)
    ctx.event(
        "image_generated",
        "تصویر تولید شد",
        {
            "imageId": image_id,
            "role": role,
            "sectionKey": section_key,
            "provider": used_cfg["provider"],
            "model": used_cfg["model"],
            "width": opt_w,
            "height": opt_h,
            "format": mime,
            "estimatedCost": cost,
            "durationMs": duration_ms,
            "retries": result.retries,
            "attempts": attempts,
            "fallbackUsed": used_cfg["provider"] != provider_cfg["provider"],
        },
    )
    return {"articleId": article_id, "imageId": image_id, "version": version, "cost": cost}


@register_job("generate_cover_image")
async def handle_generate_cover_image(ctx: JobContext) -> dict[str, Any]:
    return await _generate_image(ctx, role="cover", section_key=COVER_KEY)


@register_job("generate_interior_image")
async def handle_generate_interior_image(ctx: JobContext) -> dict[str, Any]:
    section_key = (ctx.payload().get("sectionKey") or "").strip()
    if not section_key:
        raise ValueError("generate_interior_image payload is missing sectionKey")
    return await _generate_image(ctx, role="interior", section_key=section_key)


@register_job("generate_article_image")
async def handle_generate_article_image(ctx: JobContext) -> dict[str, Any]:
    payload = ctx.payload()
    role = (payload.get("role") or "").strip()
    if role not in ("cover", "interior"):
        raise ValueError(f"invalid image role: {role!r}")
    return await _generate_image(ctx, role=role, section_key="cover" if role == "cover" else "")


# ---------------------------------------------------------------------------
# re-optimization of an existing image
# ---------------------------------------------------------------------------
@register_job("optimize_article_image")
async def handle_optimize_article_image(ctx: JobContext) -> dict[str, Any]:
    image_id = (ctx.payload().get("imageId") or "").strip()
    if not image_id:
        raise ValueError("optimize_article_image payload is missing imageId")
    repo = ArticleImageRepo(ctx.pb)
    row = repo.get(image_id)
    if not row:
        raise ValueError(f"image not found: {image_id}")
    source = row.get("sourceFile") or ""
    if not source:
        raise PermanentError(
            "این تصویر فایل منبع ندارد — برای بهینه‌سازی مجدد ابتدا آن را دوباره تولید کنید"
        )
    imgs = ctx.config.images
    ctx.stage_started("optimizing", "در حال بهینه‌سازی تصویر…")
    data = await _download_file(pb_file_url(image_id, source))
    service = ImageOptimizationService()
    optimized, mime, width, height, small = service.optimize(
        data, target_format=imgs["optimization_format"]
    )
    ext = mime.split("/", 1)[1]
    old_name = row.get("filename") or ""
    stem = old_name.rsplit(".", 1)[0] if old_name and "." in old_name else f"image-{image_id}"
    filename = f"{stem}.{ext}"
    from pocketbase.models.file_upload import FileUpload

    files: dict[str, Any] = {"optimizedFile": FileUpload((filename, optimized))}
    if small is not None:
        files["smallFile"] = FileUpload((filename.replace(f".{ext}", f"-640.{ext}"), small))
    ctx.pb.collection("article_images").update(image_id, files)
    repo.mark_ready(
        image_id,
        width=width,
        height=height,
        format=mime,
        file_size=len(optimized),
        filename=filename,
    )
    # ponytail: a re-optimized image keeps its old wordpressMediaId — WP keeps
    # serving the pre-re-optimization file until the next publish re-attaches.
    ctx.event(
        "image_optimized",
        "تصویر بهینه‌سازی شد",
        {"imageId": image_id, "format": mime, "width": width, "height": height},
    )
    return {"imageId": image_id, "format": mime, "width": width, "height": height}


# ---------------------------------------------------------------------------
# WordPress media upload (idempotent) — single image + publish integration
# ---------------------------------------------------------------------------
async def ensure_wp_media(
    ctx: JobContext, row: dict[str, Any], article: dict[str, Any], publisher: Any
) -> tuple[int, str]:
    """Upload once, reuse forever: existing wordpressMediaId short-circuits."""
    existing_id = int(row.get("wordpressMediaId") or 0)
    if existing_id:
        return existing_id, str(row.get("wordpressUrl") or "")
    file_field = row.get("optimizedFile") or row.get("sourceFile") or ""
    if not file_field:
        raise PermanentError("تصویر فایل آماده ندارد", details={"imageId": row["id"]})
    data = await _download_file(pb_file_url(row["id"], file_field))
    try:
        media = await publisher.upload_media(
            data=data,
            filename=row.get("filename") or f"image-{row['id']}.webp",
            title=row.get("altText") or (article.get("title") or ""),
            alt_text=row.get("altText") or "",
            caption=row.get("caption") or "",
            post_id=int(article.get("wordpressPostId") or 0) or None,
        )
    except ProviderError as exc:
        raise PermanentError(
            f"بارگذاری تصویر در وردپرس ناموفق بود: {exc}",
            details={"category": "wordpress_upload_failure", "imageId": row["id"]},
        ) from exc
    media_id = int(media.get("id") or 0)
    if not media_id:
        raise PermanentError(
            "wordpress media upload returned no id",
            details={"category": "wordpress_upload_failure", "imageId": row["id"]},
        )
    ArticleImageRepo(ctx.pb).set_wordpress_media(row["id"], media_id, str(media.get("url") or ""))
    return media_id, str(media.get("url") or "")


@register_job("publish_article_image")
async def handle_publish_article_image(ctx: JobContext) -> dict[str, Any]:
    image_id = (ctx.payload().get("imageId") or "").strip()
    if not image_id:
        raise ValueError("publish_article_image payload is missing imageId")
    repo = ArticleImageRepo(ctx.pb)
    row = repo.get(image_id)
    if not row:
        raise ValueError(f"image not found: {image_id}")
    article = ArticleRepo(ctx.pb).get(row.get("article") or "")
    if not article:
        raise ValueError("article not found for image")
    # fail fast: a cover can only become the featured image on a published post
    if (
        ctx.payload().get("featured")
        and row.get("role") == "cover"
        and not int(article.get("wordpressPostId") or 0)
    ):
        raise PermanentError("مقاله هنوز در وردپرس منتشر نشده — تنظیم تصویر شاخص ممکن نیست")
    publisher = ctx.providers.publisher
    media_id, url = await ensure_wp_media(ctx, row, article, publisher)
    featured = False
    if ctx.payload().get("featured") and row.get("role") == "cover":
        wp_post_id = int(article.get("wordpressPostId") or 0)
        await publisher.set_featured_media(wp_post_id, media_id)
        featured = True
    ctx.event(
        "image_uploaded",
        "تصویر در وردپرس بارگذاری شد",
        {"imageId": image_id, "mediaId": media_id, "url": url, "featured": featured},
    )
    return {"imageId": image_id, "mediaId": media_id, "url": url, "featured": featured}


_H2_RE = re.compile(r"<h2[^>]*>.*?</h2>", re.IGNORECASE | re.DOTALL)


def _insert_after_section_heading(html: str, position: int, figure: str) -> str:
    """Insert a figure right after the position-th <h2> (1-based). Missing
    section → figure appended at the end of the body so it is never lost."""
    matches = list(_H2_RE.finditer(html))
    if 0 < position <= len(matches):
        m = matches[position - 1]
        return html[: m.end()] + figure + html[m.end() :]
    return html + figure


async def apply_images_to_html(
    ctx: JobContext,
    article: dict[str, Any],
    html: str,
    publisher: Any,
    *,
    allow_missing_cover: bool = False,
) -> tuple[str, int]:
    """Publish-time image pipeline. Returns (html, featured_media_id).

    Raises PermanentError when the pre-publish image validation fails and no
    explicit override was requested (cover missing/broken blocks publishing).
    Articles without an image plan keep the legacy publish path untouched.
    """
    if not article.get("imagePlan"):
        return html, 0
    repo = ArticleImageRepo(ctx.pb)
    ready = [
        row
        for row in repo.list_for_article(article["id"], include_inactive=False)
        if row.get("status") == "ready" and row.get("optimizedFile")
    ]
    has_cover = any(row.get("role") == "cover" for row in ready)
    ok, problems = validate_publishable_images(
        ready, has_cover=has_cover, allow_missing_cover=allow_missing_cover
    )
    if not ok:
        raise PermanentError(
            "انتشار متوقف شد: " + "؛ ".join(problems),
            details={"images": problems, "article": article["id"]},
        )

    replacements: dict[str, str] = {}
    featured_media_id = 0
    for row in ready:
        media_id, url = await ensure_wp_media(ctx, row, article, publisher)
        if row.get("role") == "cover":
            featured_media_id = media_id
            if "{{IMAGE:cover}}" in html:
                replacements["cover"] = figure_html(
                    src=url,
                    alt=row.get("altText") or "",
                    width=int(row.get("width") or 0),
                    height=int(row.get("height") or 0),
                    caption=row.get("caption") or "",
                    loading="eager",
                    fetchpriority="high",
                )
        else:
            key = row.get("sectionKey") or ""
            figure = figure_html(
                src=url,
                alt=row.get("altText") or "",
                width=int(row.get("width") or 0),
                height=int(row.get("height") or 0),
                caption=row.get("caption") or "",
            )
            if f"{{{{IMAGE:{key}}}}}" in html:
                replacements[key] = figure
            else:
                # Real articles don't contain {{IMAGE:section-N}} placeholders —
                # place the figure after the section's <h2> instead (section keys
                # are 1-based positions in document order).
                try:
                    position = int(key.rsplit("-", 1)[-1])
                except ValueError:
                    position = 0
                html = _insert_after_section_heading(html, position, figure)
    if replacements:
        html = resolve_image_placeholders(html, replacements)
    return html, featured_media_id
