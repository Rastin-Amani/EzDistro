"""Image subsystem domain: plan models, fingerprints, filenames, cost, errors.

Pure logic only (no I/O). The LLM produces an :class:`ArticleImagePlan`; every
generated image is persisted as an immutable version row whose snapshot of the
plan fields (prompt/size/style hash) is what actually got generated.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

IMAGE_ROLES = ("cover", "interior")

# {{IMAGE:cover}} / {{IMAGE:section-2}} — resolved at publish time.
IMAGE_PLACEHOLDER_RE = re.compile(r"\{\{IMAGE:([a-z0-9_-]+)\}\}")
SECTION_KEY_RE = re.compile(r"^section-\d+$")

COVER_KEY = "cover"
# Interiors are inserted after the heading of their planned section.
DEFAULT_INTERIOR_SIZE = (1280, 720)
DEFAULT_COVER_SIZE = (1600, 900)
MIN_COVER_WIDTH = 1200
MAX_SOURCE_BYTES = 8 * 1024 * 1024  # source generation guard (PB file cap headroom)
MIN_SOURCE_BYTES = 5 * 1024

# Normalized error categories (job errorCode + UI human messages).
ERROR_CATEGORIES = (
    "rate_limited",
    "timeout",
    "authentication",
    "invalid_request",
    "content_policy",
    "provider_unavailable",
    "invalid_response",
    "decode_failure",
    "optimization_failure",
    "wordpress_upload_failure",
    "unknown",
)

_RETRYABLE_CATEGORIES = frozenset(
    {"rate_limited", "timeout", "provider_unavailable", "invalid_response"}
)

# Cost table: the single data-driven place for price rates (kept out of
# business logic; UI shows "unknown" when a provider has no entry or the
# provider reports no usage).
COST_RATES: dict[str, dict[str, float]] = {
    "bfl": {"per_megapixel": 0.015},
}

_CONTENT_POLICY_MARKERS = ("content policy", "safety", "blocked", "prohibited content")
_AUTH_MARKERS = ("api key", "unauthorized", "401", "forbidden", "403", "authentication")


# ---------------------------------------------------------------------------
# Plan models (LLM output shape)
# ---------------------------------------------------------------------------
class PlannedImage(BaseModel):
    """One planned image slot: cover or interior."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    role: Literal["cover", "interior"] = "interior"
    section_key: str = ""  # "" for cover; "section-N" for interiors
    purpose: str = ""
    placement: str = ""  # human hint (after h2 of section N)
    prompt: str = ""
    negative_prompt: str = ""
    aspect_ratio: str = "16:9"
    suggested_size: list[int] = Field(default_factory=lambda: list(DEFAULT_INTERIOR_SIZE))
    alt_text: str = ""
    caption: str = ""
    semantic_role: str = ""  # e.g. "diagram", "photo", "screenshot"

    @field_validator("prompt")
    @classmethod
    def _prompt_required(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("image prompt is required")
        return v.strip()

    @field_validator("section_key")
    @classmethod
    def _key_shape(cls, v: str, info: Any) -> str:
        role = (info.data or {}).get("role")
        if role == "interior" and not SECTION_KEY_RE.match(v or ""):
            raise ValueError(f"interior section_key must match section-N, got {v!r}")
        if role == "cover":
            return ""
        return (v or "").strip()

    @field_validator("suggested_size")
    @classmethod
    def _size_shape(cls, v: list[int]) -> list[int]:
        if len(v) != 2 or int(v[0]) <= 0 or int(v[1]) <= 0:
            raise ValueError(f"suggested_size must be [width, height], got {v!r}")
        return [int(v[0]), int(v[1])]

    @field_validator("aspect_ratio")
    @classmethod
    def _ar_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if not re.match(r"^\d{1,2}:\d{1,2}$", v):
            raise ValueError(f"aspect_ratio must be W:H, got {v!r}")
        return v


class ArticleImagePlan(BaseModel):
    """Validated image plan for one article (one plan version)."""

    model_config = {"extra": "ignore"}

    version: int = 1
    style: dict[str, Any] = Field(default_factory=dict)
    images: list[PlannedImage] = Field(default_factory=list)

    def cover(self) -> PlannedImage | None:
        for img in self.images:
            if img.role == "cover":
                return img
        return None

    def interiors(self) -> list[PlannedImage]:
        return [img for img in self.images if img.role == "interior"]

    def find(self, role: str, section_key: str) -> PlannedImage | None:
        """The planned spec for a slot (cover slots normalize to COVER_KEY)."""
        want = section_key or COVER_KEY if role == "cover" else section_key
        for img in self.images:
            key = img.section_key or COVER_KEY if img.role == "cover" else img.section_key
            if img.role == role and key == want:
                return img
        return None

    @classmethod
    def parse_llm(
        cls, raw: dict[str, Any], *, version: int = 1, style: dict[str, Any] | None = None
    ) -> ArticleImagePlan:
        """Normalize raw LLM JSON into a plan; raises ValidationError on junk."""
        images = raw.get("images") if isinstance(raw, dict) else None
        if not isinstance(images, list):
            raise ValueError("plan JSON must contain an 'images' list")
        plan = cls.model_validate(
            {"version": version, "style": style or raw.get("style") or {}, "images": images}
        )
        problems = validate_plan(plan)
        if problems:
            raise ValueError("; ".join(problems))
        return plan

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        return data


def validate_plan(plan: ArticleImagePlan, *, max_interior: int = 4) -> list[str]:
    """Structural rules the LLM must obey (deterministic pre-flight)."""
    problems: list[str] = []
    covers = [i for i in plan.images if i.role == "cover"]
    if len(covers) != 1:
        problems.append(f"plan must contain exactly one cover image (found {len(covers)})")
    keys = set()
    for img in plan.interiors():
        if img.section_key in keys:
            problems.append(f"duplicate interior section_key: {img.section_key}")
        keys.add(img.section_key)
    if len(plan.interiors()) > max_interior:
        problems.append(f"too many interior images ({len(plan.interiors())} > {max_interior})")
    for img in plan.images:
        if len(img.prompt) > 4000:
            problems.append(f"prompt too long for {img.role} {img.section_key} (>4000 chars)")
        if len(img.alt_text) > 300:
            problems.append(f"alt_text too long for {img.role} {img.section_key} (>300 chars)")
    return problems


# ---------------------------------------------------------------------------
# Fingerprints / filenames
# ---------------------------------------------------------------------------
def image_fingerprint(
    *,
    article_id: str,
    role: str,
    section_key: str,
    plan_version: int,
    provider: str,
    model: str,
    prompt: str,
    style_config: dict[str, Any] | str,
    width: int,
    height: int,
) -> str:
    """Stable idempotency fingerprint: identical inputs → identical hash.

    Changing ANY ingredient (prompt, style, dims, model, plan version) yields a
    different fingerprint → regeneration is a new image, never a silent reuse.
    """
    style_raw = (
        style_config
        if isinstance(style_config, str)
        else repr(sorted((style_config or {}).items()))
    )
    payload = "|".join(
        [
            article_id,
            role,
            section_key or "",
            str(plan_version),
            provider,
            model,
            prompt,
            style_raw,
            str(width),
            str(height),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def ascii_slug(text: str, fallback: str = "image") -> str:
    """ASCII-only filename fragment (SEO: stable URLs survive any locale)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def image_filename(
    *,
    article_title: str,
    article_id: str,
    role: str,
    section_key: str = "",
    ext: str = "webp",
    image_id: str = "",
) -> str:
    """Stable, ASCII, SEO-friendly filename: check-engine-light-dashboard.webp."""
    base = ascii_slug(article_title, fallback="")
    # cover slots carry section_key="cover" — never duplicate it in the name
    section_part = "" if role == COVER_KEY else ascii_slug(section_key, fallback="")
    # role word only when there is no descriptive section key
    role_part = "" if section_part else role
    parts = [p for p in (base, role_part, section_part) if p]
    if not base:
        # Persian-only title: fall back to ids so the URL stays ASCII.
        parts = [ascii_slug(f"image-{article_id[:8]}", fallback="image"), role]
    name = "-".join(parts)
    if image_id:
        name = f"{name}-{image_id[:6]}"
    return f"{name[:120]}.{ext.lstrip('.')}"


# ---------------------------------------------------------------------------
# Visual prompt composition (style-aware, locale-decoupled)
# ---------------------------------------------------------------------------
def build_visual_prompt(
    plan_prompt: str,
    style_config: dict[str, Any] | None,
    *,
    subject_context: str = "",
) -> str:
    """Final visual prompt = plan prompt + project style profile.

    The plan prompt is already in the model-effective language (the planning
    prompt instructs the LLM); style adds tone/palette/lighting constraints.
    """
    style = style_config or {}
    parts = [plan_prompt.strip()]
    if subject_context:
        parts.append(subject_context.strip())
    style_bits = "، ".join(
        bit
        for bit in (
            str(style.get("tone") or "").strip(),
            str(style.get("palette") or "").strip(),
            str(style.get("lighting") or "").strip(),
        )
        if bit
    )
    if style_bits:
        parts.append(f"Style: {style_bits}.")
    return "\n".join(p for p in parts if p)


def build_negative_prompt(style_config: dict[str, Any] | None, plan_negative: str = "") -> str:
    """Default: no rendered text in images (LLM text artifacts look broken)."""
    style = style_config or {}
    negatives = {"text, letters, words, watermark, signature, logo"}
    style_negative = str(style.get("negative_prompt") or "").strip()
    if style_negative:
        negatives.add(style_negative)
    prohibited = str(style.get("prohibited") or "").strip()
    if prohibited:
        negatives.add(prohibited)
    if plan_negative.strip():
        negatives.add(plan_negative.strip())
    return ", ".join(sorted(negatives))


def dims_for_aspect(
    aspect_ratio: str,
    *,
    role: str,
    min_width: int = 0,
) -> tuple[int, int]:
    """Deterministic generation dimensions for an aspect ratio.

    Cover gets extra headroom for SEO/Discover (>= min_width, default 1200);
    both sides are multiples of 32 (provider-safe, BFL requires it).
    """
    try:
        w_ratio, h_ratio = (int(x) for x in aspect_ratio.split(":", 1))
        if w_ratio <= 0 or h_ratio <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        w_ratio, h_ratio = 16, 9
    target_w = DEFAULT_COVER_SIZE[0] if role == "cover" else DEFAULT_INTERIOR_SIZE[0]
    if role == "cover":
        target_w = max(target_w, min_width or 0, MIN_COVER_WIDTH)
    width = int(round(target_w / 32.0)) * 32
    height = int(round(width * h_ratio / w_ratio / 32.0)) * 32
    return width, max(height, 32)


def megapixels_billed(width: int, height: int) -> int:
    """BFL-style megapixel rounding: 1920x1080 bills as 3MP (ceil)."""
    return max(1, math.ceil((width * height) / 1_000_000))


def estimate_cost(
    provider: str,
    width: int,
    height: int,
    usage: dict[str, Any] | None = None,
) -> float | None:
    """Estimated USD cost; None = unknown (UI shows 'نامشخص')."""
    rates = COST_RATES.get(provider or "")
    if not rates:
        return None
    billed_mp = None
    if usage:
        billed_mp = usage.get("billed_megapixels") or usage.get("megapixels")
    if billed_mp is None:
        billed_mp = megapixels_billed(width, height)
    return round(max(1, int(billed_mp)) * rates["per_megapixel"], 4)


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------
def classify_error(message: str, retryable: bool = False) -> str:
    """Map a provider failure message to a normalized category."""
    msg = (message or "").lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "rate_limited"
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if any(m in msg for m in _AUTH_MARKERS):
        return "authentication"
    if any(m in msg for m in _CONTENT_POLICY_MARKERS):
        return "content_policy"
    if "decode" in msg or "cannot identify image" in msg:
        return "decode_failure"
    if "optimize" in msg or "resize" in msg:
        return "optimization_failure"
    if "wordpress" in msg or "wp." in msg:
        return "wordpress_upload_failure"
    if "unavailable" in msg or "connection" in msg or "5xx" in msg:
        return "provider_unavailable"
    if "invalid" in msg or "malformed" in msg or "unexpected" in msg or "schema" in msg:
        return "invalid_request" if not retryable else "invalid_response"
    return "unknown"


def category_is_retryable(category: str) -> bool:
    return category in _RETRYABLE_CATEGORIES


# ---------------------------------------------------------------------------
# Publish-time HTML: figure markup + placeholder resolution
# ---------------------------------------------------------------------------
def figure_html(
    *,
    src: str,
    alt: str,
    width: int,
    height: int,
    caption: str = "",
    loading: str = "lazy",
    fetchpriority: str = "",
    srcset: str = "",
    sizes: str = "",
    figcaption_class: str = "",
) -> str:
    """Semantic, CLS-safe figure markup (width/height prevent layout shift)."""
    attrs = [
        f'src="{_esc(src)}"',
        f'alt="{_esc(alt)}"',
        f'width="{int(width)}"',
        f'height="{int(height)}"',
        f'loading="{_esc(loading)}"',
        'decoding="async"',
    ]
    if fetchpriority:
        attrs.append(f'fetchpriority="{_esc(fetchpriority)}"')
    if srcset:
        attrs.append(f'srcset="{_esc(srcset)}"')
    if sizes:
        attrs.append(f'sizes="{_esc(sizes)}"')
    img = f"<img {' '.join(attrs)}>"
    if caption:
        cap = f"<figcaption{_attr_class(figcaption_class)}>{_esc(caption)}</figcaption>"
        return f"<figure>{img}{cap}</figure>"
    return img


def _attr_class(cls: str) -> str:
    return f' class="{_esc(cls)}"' if cls else ""


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def placeholder_keys(html: str) -> list[str]:
    return IMAGE_PLACEHOLDER_RE.findall(html or "")


def resolve_image_placeholders(
    html: str,
    replacement: dict[str, str],
) -> str:
    """Swap {{IMAGE:key}} for figure markup; unknown keys are removed."""

    def _sub(match: re.Match[str]) -> str:
        return replacement.get(match.group(1), "")

    return IMAGE_PLACEHOLDER_RE.sub(_sub, html or "")


def validate_alt_text(
    alt: str,
    *,
    article_title: str,
    keyword: str = "",
    prompt: str = "",
    min_len: int = 10,
    max_len: int = 200,
) -> list[str]:
    """Alt-text quality rules (SEO): natural description, not stuffed/leaked."""
    problems: list[str] = []
    alt_clean = (alt or "").strip()
    if len(alt_clean) < min_len:
        problems.append("alt text too short")
    if len(alt_clean) > max_len:
        problems.append("alt text too long")
    if article_title and alt_clean.strip() == (article_title or "").strip():
        problems.append("alt text must not repeat the article title verbatim")
    if prompt and prompt.strip()[:60] and alt_clean.startswith(prompt.strip()[:60]):
        problems.append("alt text must not leak the generation prompt")
    kw = (keyword or "").strip()
    # natural use is fine; stuffing is not
    if kw and len(kw) > 3 and alt_clean.lower().count(kw.lower()) > 2:
        problems.append("alt text is keyword-stuffed")
    return problems


def validate_publishable_images(
    images: list[dict[str, Any]],
    *,
    has_cover: bool,
    allow_missing_cover: bool = False,
) -> tuple[bool, list[str]]:
    """Pre-publish gate: cover must exist + be ready; interiors are optional.

    Returns (ok, problems). A missing cover blocks publishing unless the
    caller passes an explicit override (allow_missing_cover).
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not has_cover:
        if not allow_missing_cover:
            problems.append("cover image missing — generate it or publish without images")
    else:
        for img in images:
            if img.get("role") != "cover" or not img.get("active"):
                continue
            if img.get("status") != "ready":
                problems.append("cover image is not ready yet")
            if not (img.get("optimizedFile") or img.get("sourceFile")):
                problems.append("cover has no stored file")
            if not (img.get("altText") or "").strip():
                problems.append("cover alt text is empty")
    for img in images:
        if img.get("role") != "interior" or not img.get("active"):
            continue
        if img.get("status") == "ready" and not (img.get("altText") or "").strip():
            # interior metadata is a warning, never a publish blocker —
            # an optional interior failure must not stop the article
            warnings.append(f"interior image {img.get('sectionKey') or ''} has no alt text")
    return (not problems), problems + warnings


def parse_plan_or_none(
    raw: Any, *, version: int = 1, style: dict[str, Any] | None = None
) -> ArticleImagePlan | None:
    """Tolerant plan loader: None on any malformed stored plan."""
    if not raw or not isinstance(raw, dict):
        return None
    try:
        return ArticleImagePlan.parse_llm(raw, version=version, style=style)
    except (ValidationError, ValueError):
        return None


def style_fingerprint_source(style_config: dict[str, Any] | None) -> str:
    """Canonical string form of the style config (for hashing)."""
    return repr(sorted((style_config or {}).items()))


__all__ = [
    "IMAGE_ROLES",
    "IMAGE_PLACEHOLDER_RE",
    "SECTION_KEY_RE",
    "ERROR_CATEGORIES",
    "PlannedImage",
    "ArticleImagePlan",
    "validate_plan",
    "image_fingerprint",
    "ascii_slug",
    "image_filename",
    "build_visual_prompt",
    "build_negative_prompt",
    "dims_for_aspect",
    "megapixels_billed",
    "estimate_cost",
    "classify_error",
    "category_is_retryable",
    "figure_html",
    "placeholder_keys",
    "resolve_image_placeholders",
    "validate_alt_text",
    "validate_publishable_images",
    "parse_plan_or_none",
]
