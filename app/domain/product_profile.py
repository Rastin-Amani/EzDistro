"""Product profile — brand/product context extracted from a landing page.

Facts first: the profile is produced by the ``analyze_landing_page`` job
(fetch + LLM extraction) and stored on project settings. This module only
normalizes and formats it for prompts and the article footer — it never
invents facts, and it never trusts the LLM's shape.
"""

from __future__ import annotations

import html as _html
from typing import Any

_PROFILE_KEYS = ("brand", "summary", "products", "value_props")


def _s(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_profile(raw: Any) -> dict[str, Any]:
    """Keep only known fields, bounded in size (untrusted LLM JSON)."""
    if not isinstance(raw, dict):
        return {}
    products: list[dict[str, Any]] = []
    for item in (raw.get("products") or [])[:12]:
        if not isinstance(item, dict):
            continue
        diffs = [_s(x, 160) for x in (item.get("differentiators") or []) if _s(x, 160)][:8]
        product = {
            "name": _s(item.get("name"), 160),
            "description": _s(item.get("description"), 700),
            "audience": _s(item.get("audience"), 240),
            "differentiators": diffs,
        }
        if product["name"] or product["description"]:
            products.append(product)
    return {
        "brand": _s(raw.get("brand"), 160),
        "summary": _s(raw.get("summary"), 900),
        "industry": _s(raw.get("industry"), 160),
        "products": products,
        "value_props": [_s(x, 240) for x in (raw.get("value_props") or []) if _s(x, 240)][:10],
        "keywords": [_s(x, 80) for x in (raw.get("keywords") or []) if _s(x, 80)][:20],
    }


def unwrap(stored: Any) -> dict[str, Any]:
    """Read the profile out of the stored settings wrapper (tolerant of both)."""
    if not isinstance(stored, dict):
        return {}
    profile = stored.get("profile")
    if isinstance(profile, dict):
        return profile
    if any(key in stored for key in _PROFILE_KEYS):
        return stored
    return {}


def format_product_profile(stored: Any) -> str:
    """Prompt-ready product context; empty string when there is nothing to say."""
    profile = normalize_profile(unwrap(stored))
    if not any(
        profile.get(key)
        for key in ("brand", "summary", "industry", "products", "value_props", "keywords")
    ):
        return ""
    lines = [
        "## BRAND / PRODUCT CONTEXT",
        "Ground comparisons, recommendations, examples and buying guidance in the "
        "brand and products below. This is the only product data you may treat as fact "
        "— do not invent features, prices or claims beyond it.",
    ]
    if profile["brand"]:
        lines.append(f"Brand: {profile['brand']}")
    if profile["industry"]:
        lines.append(f"Industry: {profile['industry']}")
    if profile["summary"]:
        lines.append(f"Summary: {profile['summary']}")
    if profile["value_props"]:
        lines.append("Value propositions:")
        lines.extend(f"- {v}" for v in profile["value_props"])
    if profile["products"]:
        lines.append("Products:")
        for product in profile["products"]:
            bits = [product["name"] or "Product"]
            if product["description"]:
                bits.append(product["description"])
            lines.append("- " + " — ".join(bits))
            if product["audience"]:
                lines.append(f"  Audience: {product['audience']}")
            for diff in product["differentiators"]:
                lines.append(f"  Differentiator: {diff}")
    if profile["keywords"]:
        lines.append("Related terms: " + ", ".join(profile["keywords"]))
    return "\n".join(lines)


def product_footer_html(stored: Any, landing_url: str = "") -> str:
    """A short, factual 'About the brand' paragraph for the end of an article.

    Returns "" when there is no usable profile. Kept deliberately short and
    attributed so it reads as a publisher note, not as hidden promotion.
    """
    profile = normalize_profile(unwrap(stored))
    brand = profile.get("brand") or ""
    summary = profile.get("summary") or ""
    if not (brand or summary):
        return ""
    parts: list[str] = []
    if summary:
        parts.append(_html.escape(summary))
    url = (landing_url or "").strip()
    if brand and url:
        parts.append(
            f'Learn more at <a href="{_html.escape(url, quote=True)}">{_html.escape(brand)}</a>.'
        )
    elif brand:
        parts.append(_html.escape(brand) + ".")
    body = " ".join(parts)
    heading = f"About {_html.escape(brand)}" if brand else "About"
    return f"<p><strong>{heading}</strong>: {body}</p>"
