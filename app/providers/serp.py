"""Optional SERP providers.

`none` is a first-class provider: with no SERP provider configured, the research
engine runs in "Google Ads + site intelligence" mode and every SERP-specific
field simply reads as uncollected. Nothing is faked to look like ranking data.

Serper.dev is implemented because it is a cheap, plain JSON Google SERP API that
works with the httpx client already in the project. Adding another provider is a
new class + a registry entry — the research engine depends only on the
`SERPProvider` protocol.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from app.providers.base import (
    PermanentError,
    SERPObservation,
    SERPOrganicResult,
    TransientError,
)

logger = logging.getLogger(__name__)


class NoneSERPProvider:
    """The `none` provider: always reports "no SERP data for this market"."""

    category = "serp"
    provider_name = "none"
    PROVIDER_META: dict[str, Any] = {
        "description": "No SERP provider — ranking, PAA and SERP features stay unavailable",
        "requires_api_key": False,
        "supports_model_listing": False,
    }

    @property
    def available(self) -> bool:
        return False

    async def search(self, **_: Any) -> SERPObservation | None:
        return None

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class SerperSERPProvider:
    """serper.dev Google SERP API (https://google.serper.dev/search)."""

    category = "serp"
    provider_name = "serper"
    PROVIDER_META: dict[str, Any] = {
        "description": "serper.dev Google SERP data (organic results, PAA, related searches)",
        "requires_api_key": True,
        "supports_model_listing": False,
        "default_base_url": "https://google.serper.dev",
    }

    def __init__(
        self,
        *,
        base_url: str = "https://google.serper.dev",
        api_key: str = "",
        attempts: int = 3,
        timeout: float = 45.0,
    ) -> None:
        if not api_key:
            raise PermanentError("SERP integration is missing its API key")
        self.base_url = base_url.rstrip("/")
        self.attempts = max(1, attempts)
        self.timeout = timeout
        self._api_key = api_key
        self._client: Any | None = None

    @property
    def available(self) -> bool:
        return True

    async def _http(self) -> Any:
        if self._client is None:
            from app.providers.http import acquire_async_client

            self._client = acquire_async_client(
                self.base_url,
                auth_header=f"Bearer {self._api_key}",
                timeout=self.timeout,
            )
        return self._client

    async def search(
        self,
        *,
        keyword: str,
        locale: str = "en",
        location: str = "",
        device: str = "desktop",
    ) -> SERPObservation | None:
        from app.providers.http import raise_for_provider, with_retry

        gl, _, hl = (locale or "en").partition("-")
        payload: dict[str, Any] = {"q": keyword, "gl": gl or "us", "hl": hl or gl or "en"}
        if location:
            payload["location"] = location
        if device:
            payload["device"] = device
        client = await self._http()

        async def call() -> dict[str, Any]:
            response = await client.post(
                "/search",
                json=payload,
                headers={"X-API-KEY": self._api_key},
            )
            raise_for_provider(response, what="serp search")
            return response.json() or {}

        data = await with_retry(call, attempts=self.attempts, what="serp search")
        return parse_serper_response(keyword, data, locale=locale, location=location, device=device)

    async def ping(self) -> None:
        observation = await self.search(keyword="test", locale="en-US")
        if observation is None:
            raise TransientError("SERP provider returned no response")

    async def aclose(self) -> None:
        if self._client is not None:
            from app.providers.http import release_async_client

            release_async_client(self._client)
            self._client = None


def parse_serper_response(
    keyword: str,
    data: dict[str, Any],
    *,
    locale: str = "en-US",
    location: str = "",
    device: str = "desktop",
) -> SERPObservation:
    """Map a Serper payload to the normalized observation shape."""
    results: list[SERPOrganicResult] = []
    for item in data.get("organic") or []:
        link = str(item.get("link") or "")
        if not link:
            continue
        results.append(
            SERPOrganicResult(
                position=int(item.get("position") or len(results) + 1),
                url=link,
                domain=urlparse(link).netloc.lower().removeprefix("www."),
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
            )
        )
    questions = [
        str(q.get("question") or "") for q in (data.get("peopleAlsoAsk") or []) if q.get("question")
    ]
    related = [
        str(r.get("query") or "") for r in (data.get("relatedSearches") or []) if r.get("query")
    ]
    features: list[str] = []
    for key, label in (
        ("answerBox", "featured_snippet"),
        ("knowledgeGraph", "knowledge_panel"),
        ("peopleAlsoAsk", "people_also_ask"),
        ("relatedSearches", "related_searches"),
        ("shopping", "shopping"),
        ("localPack", "local_pack"),
        ("images", "images"),
        ("videos", "videos"),
    ):
        if data.get(key):
            features.append(label)
    return SERPObservation(
        keyword=keyword,
        provider="serper",
        locale=locale,
        location=location,
        device=device,
        results=results,
        features=features,
        questions=questions,
        related_searches=related,
        raw=data,
    )


def _selfcheck() -> None:
    payload = {
        "organic": [
            {"position": 1, "title": "A", "link": "https://www.example.com/a", "snippet": "s"},
            {"title": "B", "link": "https://other.io/b"},
            {"title": "no link"},
        ],
        "peopleAlsoAsk": [{"question": "How much?"}],
        "relatedSearches": [{"query": "gym crm"}],
        "answerBox": {"snippet": "x"},
    }
    obs = parse_serper_response("gym software", payload)
    assert len(obs.results) == 2
    assert obs.results[0].domain == "example.com"
    assert obs.results[1].position == 2
    assert obs.questions == ["How much?"]
    assert obs.related_searches == ["gym crm"]
    assert "featured_snippet" in obs.features and "people_also_ask" in obs.features


if __name__ == "__main__":
    _selfcheck()
    print("serp selfcheck ok")
