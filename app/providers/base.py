"""Provider interfaces + failure taxonomy + shared data contracts.

Dependency inversion: domain/jobs code depends ONLY on these protocols.
Concrete adapters live in providers/* and are resolved by providers/registry.py
by provider name — swapping Cohere for another embedding service means adding
an adapter, never touching the indexing system.

Every adapter exposes:
- `provider_name` / `model_name` / `model_info` (metadata for observability + UI)
- `timeout` (seconds)
- `ping()` health probe
- call observability via the metrics mixin (see providers/metrics.py)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """External provider failure, classified for the retry engine."""

    def __init__(
        self, message: str, *, retryable: bool = True, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "message": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }


class TransientError(ProviderError):
    """Network, timeout, 429, 5xx — safe to retry."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, retryable=True, details=details)


class PermanentError(ProviderError):
    """4xx, schema/validation, auth — retrying will not help."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, retryable=False, details=details)


# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------
@dataclass
class ModelInfo:
    provider: str
    name: str
    dimensions: int | None = None
    supports_streaming: bool = False
    supports_json: bool = True
    description: str = ""


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------
@dataclass
class GenerationParams:
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: float = 120.0  # per-call timeout (seconds)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens, completion_tokens
    latency_ms: int = 0
    retries: int = 0


@dataclass
class VectorPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankResult:
    index: int
    score: float
    document: str = ""  # original document text (metadata preservation)
    metadata: dict[str, Any] | None = None


@dataclass
class WPPost:
    id: int
    title: str
    content_html: str
    link: str
    status: str
    modified: str = ""
    # Sync-only extras (populated when the caller requests SYNC_FIELDS).
    slug: str = ""
    excerpt: str = ""
    date: str = ""
    modified_gmt: str = ""
    author: int = 0
    featured_media: int = 0
    categories: list[int] = field(default_factory=list)
    tags: list[int] = field(default_factory=list)


@dataclass
class PublishResult:
    post_id: int
    link: str
    status_code: int = 200


@dataclass
class ImageRequest:
    """Normalized image-generation request (validated upstream in the domain)."""

    prompt: str
    negative_prompt: str = ""
    width: int = 1280
    height: int = 720
    aspect_ratio: str = "16:9"
    output_format: str = "png"  # png | jpeg | webp
    seed: int | None = None
    reference_images: list[bytes] = field(default_factory=list)  # untrusted bytes
    quality: str = "standard"


@dataclass
class ImageResult:
    data: bytes
    mime_type: str
    width: int = 0  # 0 = unknown; the quality gate decodes and fills actuals
    height: int = 0
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)  # provider-native usage
    latency_ms: int = 0
    retries: int = 0


# ---------------------------------------------------------------------------
# Provider protocols
# ---------------------------------------------------------------------------
class LLMProvider(Protocol):
    provider_name: str
    model_name: str
    model_info: ModelInfo
    timeout: float

    async def generate(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> LLMResult: ...

    async def generate_json(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> dict[str, Any]:
        """Structured JSON generation — returns a raw dict (schema validation
        stays in the domain layer)."""
        ...

    async def stream(
        self,
        *,
        system: str | None,
        user: str,
        params: GenerationParams | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas as they arrive."""
        ...

    async def ping(self) -> None: ...

    async def list_models(self) -> list[str]:
        """Available model ids; [] when the provider API has no model listing."""
        ...


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    dimensions: int
    model_info: ModelInfo

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed content to be indexed (e.g. Cohere input_type=search_document)."""
        ...

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed retrieval queries (e.g. Cohere input_type=search_query)."""
        ...

    async def ping(self) -> None: ...


class RerankerProvider(Protocol):
    provider_name: str
    model_name: str
    model_info: ModelInfo

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[RerankResult]:
        """Rank documents by relevance to query; scores + original documents
        (and caller-provided metadata) are preserved on the results."""
        ...

    async def ping(self) -> None: ...


class VectorStoreProvider(Protocol):
    provider_name: str

    async def ensure_collection(self, dimensions: int) -> None: ...
    async def delete_collection(self) -> None: ...
    async def upsert(self, points: list[VectorPoint]) -> None: ...
    async def delete(self, point_ids: list[str]) -> None: ...
    async def update_payload(self, point_ids: list[str], payload: dict[str, Any]) -> None:
        """Update vector payloads WITHOUT touching the vectors (metadata-only changes)."""
        ...

    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        threshold: float | None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Nearest-neighbour query with optional payload filter (exact matches)."""
        ...

    async def count(self, filters: dict[str, Any] | None = None) -> int: ...
    async def scroll_ids(
        self, filters: dict[str, Any] | None = None, limit: int = 1000
    ) -> list[tuple[str, dict[str, Any]]]:
        """All (point_id, payload) matching the filter — for stale cleanup."""
        ...

    async def ping(self) -> None: ...


class ImageGenerationProvider(Protocol):
    """Text-to-image adapters (Gemini image, BFL FLUX, …)."""

    provider_name: str
    model_name: str
    model_info: ModelInfo
    timeout: float

    async def generate_image(self, request: ImageRequest) -> ImageResult: ...

    async def ping(self) -> None: ...


class PublisherProvider(Protocol):
    provider_name: str

    async def list_posts(
        self,
        *,
        per_page: int,
        after_id: int | None,
        status: str,
        fields: list[str] | None = None,
    ) -> list[WPPost]: ...
    async def create_post(
        self,
        *,
        title: str,
        html: str,
        status: str,
        slug: str,
        meta: dict[str, Any] | None = None,
        excerpt: str = "",
    ) -> PublishResult: ...
    async def update_post(
        self,
        post_id: int,
        *,
        title: str | None = None,
        html: str | None = None,
        status: str | None = None,
        slug: str | None = None,
        meta: dict[str, Any] | None = None,
        excerpt: str | None = None,
    ) -> PublishResult: ...
    async def get_post(self, post_id: int) -> WPPost | None: ...
    async def unpublish_post(self, post_id: int) -> PublishResult:
        """Safely remove a post from public view (WP: status → private)."""
        ...

    async def list_categories(self) -> list[dict[str, Any]]: ...
    async def list_tags(self) -> list[dict[str, Any]]: ...

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
        """Upload binary media; returns {id, url, mime_type}. Idempotency
        (dedupe on retry) is the caller's job via a stored media id."""
        ...

    async def set_featured_media(self, post_id: int, media_id: int) -> PublishResult:
        """Attach media as the post featured image (WP featured_media)."""
        ...

    async def ping(self) -> None: ...


# =============================================================================
# SEO research providers
# =============================================================================


@dataclass
class KeywordIdea:
    """One keyword-idea row from a keyword research provider.

    Metrics are *observed* values straight from the provider. Nothing here is
    invented: `competition` is Google Ads / paid competition, never an organic
    difficulty score.
    """

    text: str
    avg_monthly_searches: int = 0
    competition: str = "UNSPECIFIED"  # LOW | MEDIUM | HIGH | UNSPECIFIED
    competition_index: int = 0
    average_cpc_micros: int = 0
    low_top_of_page_bid_micros: int = 0
    high_top_of_page_bid_micros: int = 0
    currency_code: str = ""
    # (year, month, monthly_searches) — discrete monthly observations
    monthly_volumes: list[tuple[int, int, int]] = field(default_factory=list)
    source: str = ""


@dataclass
class SEEDKind:
    """Seed kinds understood by keyword research providers (Google Ads)."""

    KEYWORD = "keyword"
    URL = "url"
    SITE = "site"
    KEYWORD_AND_URL = "keyword_and_url"


@dataclass
class SERPOrganicResult:
    position: int
    url: str = ""
    domain: str = ""
    title: str = ""
    snippet: str = ""
    is_featured_snippet: bool = False
    is_people_also_ask: bool = False


@dataclass
class SERPObservation:
    """A time-stamped SERP observation. Never a permanent fact."""

    keyword: str
    provider: str
    locale: str = ""
    location: str = ""
    device: str = "desktop"
    results: list[SERPOrganicResult] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)  # People Also Ask
    related_searches: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""


@dataclass
class AdsCustomer:
    customer_id: str
    descriptive_name: str = ""
    currency_code: str = ""
    time_zone: str = ""
    manager_customer_id: str = ""
    is_manager: bool = False
    status: str = ""


class KeywordResearchProvider(Protocol):
    """Google Ads Keyword Planning (and any future replacement)."""

    provider_name: str

    async def list_accessible_customers(self) -> list[str]: ...

    async def list_customers(self, customer_id: str | None = None) -> list[AdsCustomer]: ...

    async def generate_keyword_ideas(
        self,
        *,
        customer_id: str,
        seed_kind: str,
        keywords: list[str] | None = None,
        url: str = "",
        language: str = "languageConstants/1000",
        geo_targets: list[str] | None = None,
        network: str = "GOOGLE_SEARCH",
        include_adult_keywords: bool = False,
        page_size: int = 1000,
        max_results: int = 0,
    ) -> list[KeywordIdea]: ...

    async def ping(self) -> None: ...


class SERPProvider(Protocol):
    """Optional live-SERP provider. `none` is a first-class implementation."""

    provider_name: str

    async def search(
        self,
        *,
        keyword: str,
        locale: str = "en",
        location: str = "",
        device: str = "desktop",
    ) -> SERPObservation | None: ...

    async def ping(self) -> None: ...
