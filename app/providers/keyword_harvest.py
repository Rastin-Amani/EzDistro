"""Free keyword expansion from public autocomplete endpoints.

Why: Keyword Planner needs an Ads account, and its export only answers for the
seeds you already thought of. The autocomplete endpoints behind the search boxes
of Google, YouTube, Bing, DuckDuckGo and Amazon answer with queries people
actually type, keylessly and for free — that is where long-tail and
question-shaped demand comes from.

What it does NOT do: it never returns a search volume, competition or CPC. Those
are measured facts and this source has none, so every idea here carries zero
volume and the pipeline ranks on intent, gap and offer until the same keywords
come back with measured metrics (Keyword Planner "Get search volume", or a
previous run of this project — the harvester borrows those, see
``harvest_keywords``).

This provider holds no credentials, so it is not an Integrations category: there
is nothing to configure and nothing to test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.domain.keywords import content_tokens, normalize_keyword
from app.providers.base import KeywordIdea
from app.providers.http import build_async_client

PROVIDER_NAME = "autocomplete"
CATEGORY = "keyword_source"

GOOGLE_SUGGEST = "https://suggestqueries.google.com/complete/search"
BING_SUGGEST = "https://api.bing.com/osjson.aspx"
DDG_SUGGEST = "https://duckduckgo.com/ac/"
AMAZON_SUGGEST = "https://completion.amazon.com/api/2017/suggestions"

# Query shapes appended to every seed. Question words and evaluative words are
# what surface the phrasings answer engines quote; a bare seed only ever returns
# more of the same head term.
MODIFIERS: tuple[str, ...] = (
    "",
    "how",
    "how to",
    "what",
    "why",
    "can",
    "is",
    "does",
    "best",
    "vs",
    "for small business",
    "without",
    "free",
    "cost",
    "price",
    "alternative",
    "reviews",
    "for beginners",
)

# One client, several hosts: absolute URLs win over the base URL.
BASE_URL = "https://suggestqueries.google.com"

DEFAULT_MAX_REQUESTS = 600
REQUEST_DELAY = 0.05  # be a polite guest: these are public, undocumented endpoints
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _variants(pair: Any) -> list[str]:
    """Google/Bing shape: [query, [suggestions], …]."""
    if isinstance(pair, list) and len(pair) > 1 and isinstance(pair[1], list):
        return [str(item) for item in pair[1]]
    return []


class AutocompleteHarvester:
    """Keyless keyword source: seeds in, real query phrasings out."""

    category = CATEGORY
    provider_name = PROVIDER_NAME
    PROVIDER_META: dict[str, Any] = {
        "description": "Free autocomplete expansion (Google, YouTube, Bing, DuckDuckGo, Amazon) — no account, no volume data",
        "requires_api_key": False,
        "supports_model_listing": False,
    }

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        delay: float = REQUEST_DELAY,
        locale: str = "en-US",
        transport: Any = None,
    ) -> None:
        self._client = build_async_client(BASE_URL, timeout=timeout, transport=transport)
        self._client.headers["User-Agent"] = USER_AGENT
        self.max_requests = max(1, int(max_requests))
        self.delay = max(0.0, float(delay))
        self.locale = locale or "en-US"
        self.requests = 0
        self.failures = 0
        self.by_source: dict[str, int] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ sources
    async def _fetch(self, source: str, seed: str, modifier: str) -> list[str]:
        query = f"{seed} {modifier}".strip()
        language, _, region = self.locale.partition("-")
        headers = {"Accept": "application/json"}
        try:
            if source == "google" or source == "youtube":
                params: dict[str, Any] = {"client": "firefox", "q": query}
                if source == "youtube":
                    params["ds"] = "yt"
                params["hl"] = language or "en"
                params["gl"] = (region or "US").lower()
                response = await self._client.get(GOOGLE_SUGGEST, params=params, headers=headers)
                return _variants(response.json())
            if source == "bing":
                response = await self._client.get(
                    BING_SUGGEST, params={"query": query}, headers=headers
                )
                return _variants(response.json())
            if source == "duckduckgo":
                response = await self._client.get(
                    DDG_SUGGEST, params={"q": query, "type": "list"}, headers=headers
                )
                payload = response.json()
                if isinstance(payload, list):
                    # {"phrase": …} objects, or a bare query when it is already known
                    return [
                        str(item.get("phrase") or "") if isinstance(item, dict) else str(item)
                        for item in payload
                    ]
                return []
            if source == "amazon":
                response = await self._client.get(
                    AMAZON_SUGGEST,
                    params={
                        "mid": "ATVPDKIKX0DER",
                        "alias": "aps",
                        "prefix": query,
                        "site-variant": "desktop",
                        "client-info": "amazon-search-ui",
                        "lop": "en_US",
                    },
                    headers=headers,
                )
                payload = response.json()
                return [str(item.get("value") or "") for item in payload.get("suggestions") or []]
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            self.failures += 1
        return []

    # --------------------------------------------------------------------- API
    async def generate_keyword_ideas(
        self,
        *,
        keywords: list[str],
        max_results: int = 0,
        sources: tuple[str, ...] = ("google", "youtube", "bing", "duckduckgo", "amazon"),
    ) -> list[KeywordIdea]:
        """Expand every seed across the sources and modifiers.

        Bounded by ``max_requests`` (a run must not hammer public endpoints) and
        by ``max_results`` when the caller wants a cap. A seed only contributes
        suggestions that share a content token with it — autocomplete otherwise
        wanders off-topic, and an off-topic keyword becomes an off-topic page.
        """
        ideas: dict[str, KeywordIdea] = {}
        seeds = [seed.strip() for seed in keywords if seed and seed.strip()]

        def result() -> list[KeywordIdea]:
            values = list(ideas.values())
            return values[:max_results] if max_results > 0 else values

        for seed in seeds:
            seed_tokens = set(content_tokens(seed))
            for modifier in MODIFIERS:
                for source in sources:
                    if self.requests >= self.max_requests:
                        return result()
                    # A bare seed goes to every source; modifiers are the depth.
                    if modifier and source in ("duckduckgo", "amazon"):
                        continue
                    self.requests += 1
                    for text in await self._fetch(source, seed, modifier):
                        cleaned = text.strip()
                        key = normalize_keyword(cleaned)
                        if not key or key in ideas:
                            continue
                        if seed_tokens and not seed_tokens & set(content_tokens(cleaned)):
                            continue  # on-topic guard: ignore the wanderers
                        ideas[key] = KeywordIdea(text=cleaned, source=f"autocomplete:{source}")
                        self.by_source[source] = self.by_source.get(source, 0) + 1
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    if max_results > 0 and len(ideas) >= max_results:
                        return result()
        return result()

    async def ping(self) -> None:
        """Health probe: one seed, one source."""
        found = await self._fetch("google", "test", "")
        if not found:
            raise RuntimeError("autocomplete endpoint returned no suggestions")
