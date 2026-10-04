"""Landing-page analysis job — turn the brand's landing page into product context.

Flow (job type ``analyze_landing_page``):

1. read the project's ``landingPageUrl``
2. fetch it (SSRF-guarded) and extract the visible text
3. ask the LLM to extract a structured product profile
4. store the profile on project settings, where every prompt picks it up

Facts first: the model only summarizes the fetched page; it never invents
volumes, prices or claims. The raw profile is normalized before storage.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from app.domain.parsing import extract_json
from app.domain.product_profile import normalize_profile
from app.domain.validation import UnsafeUrlError, validate_url
from app.jobs.context import JobCancelled, JobContext
from app.jobs.handlers import register_job
from app.providers.base import GenerationParams, PermanentError, TransientError
from app.repositories.projects import ProjectSettingsRepo

_MAX_HTML_CHARS = 1_500_000
_MAX_TEXT_CHARS = 20_000

# Internal extraction prompt (not user-editable): small, fixed, factual.
_SYSTEM = (
    "You extract a factual product profile from a company landing page. "
    "Use ONLY information present in the page. Never invent prices, metrics, "
    "features or claims. Reply with JSON only."
)
_USER = """Analyse this landing page and return its product profile as JSON.

URL: {url}

PAGE TEXT:
{text}

Return ONLY this JSON shape:
{{
  "brand": "company or product name",
  "summary": "2-4 sentences on what the company sells and to whom",
  "industry": "short category",
  "products": [
    {{
      "name": "product name",
      "description": "what it is and what problem it solves",
      "audience": "who it is for",
      "differentiators": ["concrete differentiator"]
    }}
  ],
  "value_props": ["strongest value proposition"],
  "keywords": ["term the brand is associated with"]
}}

If a field is not present on the page, use an empty string or empty list.
Do not guess."""


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/chrome noise."""

    _SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    _BLOCK = {
        "p",
        "div",
        "section",
        "article",
        "li",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "td",
        "th",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n", raw)
        return raw.strip()[:_MAX_TEXT_CHARS]


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    with contextlib.suppress(Exception):
        extractor.feed(html[:_MAX_HTML_CHARS])
        extractor.close()
    return extractor.text()


async def fetch_landing_page(url: str) -> tuple[str, str]:
    """Fetch a public landing page (SSRF-guarded) → (final_url, visible text)."""
    try:
        safe_url = validate_url(url)
    except UnsafeUrlError as exc:
        raise PermanentError(f"landing page URL rejected: {exc}") from exc
    headers = {
        "User-Agent": "EzDistroBot/1.0 (+landing-page-analysis)",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            response = await client.get(safe_url, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"could not fetch the landing page: {exc}") from exc
    if response.status_code in (429,) or response.status_code >= 500:
        raise TransientError(f"landing page returned HTTP {response.status_code}")
    if response.status_code >= 400:
        raise PermanentError(f"landing page returned HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "text" not in content_type:
        raise PermanentError("landing page did not return HTML")
    text = html_to_text(response.text)
    if len(text) < 50:
        raise PermanentError("the landing page had too little readable text to analyse")
    return str(response.url), text


async def _extract_profile(ctx: JobContext, url: str, text: str) -> dict[str, Any]:
    params = GenerationParams(temperature=0.1, max_tokens=2000, timeout=120.0)
    result = await ctx.providers.llm_for("outline").generate(
        system=_SYSTEM, user=_USER.format(url=url, text=text), params=params
    )
    try:
        data = extract_json(result.text)
    except ValueError as exc:
        raise PermanentError("landing page analysis returned no valid JSON") from exc
    if not isinstance(data, dict):
        raise PermanentError("landing page analysis returned an unexpected shape")
    return normalize_profile(data)


def _store(ctx: JobContext, payload: dict[str, Any]) -> None:
    payload = {**payload, "updatedAt": datetime.now(UTC).isoformat()}
    ProjectSettingsRepo(ctx.pb).upsert(ctx.project_id, {"productProfile": payload})


@register_job("analyze_landing_page")
async def handle_analyze_landing_page(ctx: JobContext) -> dict[str, Any]:
    settings = ProjectSettingsRepo(ctx.pb).get_for_project(ctx.project_id)
    url = str(settings.get("landingPageUrl") or "").strip()
    if not url:
        raise PermanentError("No landing page URL is configured for this project — save one first.")
    _store(ctx, {"status": "running", "source": url})
    ctx.stage_started("analyzing", ("Reading the landing page…"))
    ctx.progress(20, stage="analyzing", message=("Fetching the landing page…"))
    await ctx.check_cancelled()
    try:
        final_url, text = await fetch_landing_page(url)
        ctx.progress(60, stage="analyzing", message=("Extracting the product profile…"))
        profile = await _extract_profile(ctx, final_url, text)
        if not profile.get("summary") and not profile.get("products"):
            raise PermanentError("The landing page did not yield a usable product profile")
        _store(ctx, {"status": "ready", "source": final_url, "profile": profile})
        ctx.stage_completed("analyzing", ("Product profile ready"))
        ctx.progress(100, stage="done", message=("Product profile ready"))
        return {
            "projectId": ctx.project_id,
            "status": "ready",
            "products": len(profile.get("products") or []),
            "source": final_url,
        }
    except JobCancelled:
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            _store(ctx, {"status": "failed", "source": url, "error": str(exc)[:1000]})
        raise
