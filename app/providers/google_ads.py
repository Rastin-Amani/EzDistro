"""Google Ads Keyword Planning adapter (official REST/JSON interface).

Why REST + httpx and not the google-ads gRPC library: the REST interface is
officially supported, needs no protobuf/gRPC toolchain, and adds zero
dependencies (httpx is already the project's HTTP client).

Auth model (2026): developer tokens were sunset 2026-09-09 — API access level
now rides on the Google Cloud project that owns the OAuth client, so there is
no end-user developer token to collect. We still forward a `developer-token`
header when the app sets one, for back-compat (Google ignores it now).

Nothing in this module invents metrics. Every number returned is the value
Google returned. `competition` is Google Ads (paid) competition and is never
presented as organic/SEO difficulty.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.providers.base import (
    AdsCustomer,
    KeywordIdea,
    PermanentError,
    TransientError,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_ADS_HOST = "https://googleads.googleapis.com"

# Google Ads API error status → human-readable, actionable message.
_HUMAN_ERRORS = {
    "RESOURCE_EXHAUSTED": (
        "Google Ads temporarily limited this request. EZDistro will retry automatically."
    ),
    "PERMISSION_DENIED": (
        "Your connected Google Ads account does not currently allow EZDistro to read this data."
    ),
    "UNAUTHENTICATED": (
        "Google Ads rejected the request. Check that the Google Ads connection has "
        "a valid developer token and that the Google Cloud project has Google Ads API "
        "access — or reconnect the account."
    ),
    "UNAVAILABLE": "Google Ads was unreachable. EZDistro will retry automatically.",
    "DEADLINE_EXCEEDED": "Google Ads took too long to respond. EZDistro will retry automatically.",
    "INTERNAL": "Google Ads reported an internal error. EZDistro will retry automatically.",
    "INVALID_ARGUMENT": "Google Ads rejected the request — check the research targeting settings.",
    "QUOTA_ERROR": "The Google Ads API quota for this project is exhausted. Try again later.",
}

_TRANSIENT_STATUSES = {"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "RESOURCE_EXHAUSTED"}


class GoogleAdsClient:
    """Keyword Planning + customer discovery for one Google Ads connection."""

    provider_name = "google_ads"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        developer_token: str = "",
        api_version: str = "v25",
        login_customer_id: str = "",
        timeout: float = 60.0,
        attempts: int = 3,
        concurrency: int = 2,
    ) -> None:
        if not client_id or not client_secret:
            raise PermanentError("Google Ads OAuth client is not configured (app settings)")
        if not refresh_token:
            raise PermanentError("Google Ads connection has no refresh token")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._developer_token = developer_token
        self.api_version = api_version
        self._login_customer_id = login_customer_id
        self.timeout = timeout
        self.attempts = max(1, attempts)
        # Keyword Planning enforces tight per-project rate limits; a process
        # wide semaphore keeps concurrency low without a queue service.
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=f"{_ADS_HOST}/{self.api_version}",
            timeout=httpx.Timeout(timeout, connect=15.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    # -- auth ------------------------------------------------------------------
    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                        "grant_type": "refresh_token",
                    },
                    headers={"Accept": "application/json"},
                )
            if resp.status_code in (400, 401):
                raise PermanentError(
                    "Google Ads authorization was rejected — reconnect Google Ads "
                    "(the refresh token may have been revoked).",
                    {"status": resp.status_code},
                )
            if not resp.is_success:
                raise TransientError(
                    f"Google token refresh failed with HTTP {resp.status_code}",
                    {"status": resp.status_code},
                )
            data = resp.json()
            token = str(data.get("access_token") or "")
            if not token:
                raise PermanentError("Google token refresh returned no access_token")
            expires_in = int(data.get("expires_in") or 3600)
            self._token = token
            # refresh a minute early so a slow call never trips on expiry
            self._token_expiry = time.monotonic() + max(60, expires_in - 60)
            return token

    def _headers(self, customer_id: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._developer_token:
            headers["developer-token"] = self._developer_token
        login = self._login_customer_id or (customer_id or "")
        if login:
            headers["login-customer-id"] = login.replace("customers/", "")
        return headers

    # -- error mapping ---------------------------------------------------------
    @staticmethod
    def _classify(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        status = response.status_code
        message = response.text[:500]
        api_status = ""
        error_code = ""
        request_id = response.headers.get("request-id", "")
        try:
            error = (response.json() or {}).get("error") or {}
            api_status = str(error.get("status") or "")
            message = str(error.get("message") or message)
            details = error.get("details") or []
            for block in details:
                for item in (block or {}).get("errors") or []:
                    if not error_code:
                        codes = item.get("errorCode") or {}
                        if isinstance(codes, dict):
                            error_code = str(next(iter(codes.values()), "") or "")
                    if item.get("message"):
                        message = str(item["message"])
                        break
        except (ValueError, AttributeError):
            pass
        human = _HUMAN_ERRORS.get(api_status, "")
        # The API's own message is far more specific than the generic mapping
        # (e.g. UNAUTHENTICATED usually means "no Ads API access level / missing
        # developer token", NOT an expired token). Keep both: human first for
        # the user, the raw message in details for debugging.
        text = human or message
        details_out: dict[str, Any] = {
            "status": status,
            "google_status": api_status,
            "google_message": message,
        }
        if error_code:
            details_out["google_error_code"] = error_code
        if request_id:
            details_out["request_id"] = request_id
        if api_status in _TRANSIENT_STATUSES or status == 429 or 500 <= status <= 599:
            raise TransientError(f"{what}: {text}", details_out)
        raise PermanentError(f"{what}: {text}", details_out)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        customer_id: str | None = None,
    ) -> Any:
        from app.providers.http import with_retry

        async def call() -> Any:
            async with self._sem:
                token = await self._access_token()
                headers = self._headers(customer_id)
                headers["Authorization"] = f"Bearer {token}"
                response = await self._http.request(method, path, json=json, headers=headers)
            self._classify(response, path.split(":")[-1])
            return response.json() if response.content else {}

        return await with_retry(call, attempts=self.attempts, what=f"google_ads {path}")

    # -- customer discovery ----------------------------------------------------
    async def list_accessible_customers(self) -> list[str]:
        data = await self._request("GET", "/customers:listAccessibleCustomers")
        names = data.get("resourceNames") or []
        return [str(n).replace("customers/", "") for n in names]

    async def list_customers(self, customer_id: str | None = None) -> list[AdsCustomer]:
        """customer_client metadata for a (manager or direct) customer id.

        With no explicit/manager customer id we list every accessible customer
        and enrich each with metadata. Metadata is best-effort: a customer whose
        `customer_client` query is denied (e.g. a direct account that needs no
        manager) still appears, using the accessible-customer id itself — so a
        single direct Ads account is never dropped just because the metadata
        probe failed.
        """
        target = (customer_id or self._login_customer_id or "").replace("customers/", "")
        if target:
            return await self._customer_metadata(target)

        out: list[AdsCustomer] = []
        emitted: set[str] = set()
        probed: set[str] = set()
        for cid in await self.list_accessible_customers():
            cid = str(cid).replace("customers/", "")
            if not cid or cid in probed:
                continue
            probed.add(cid)
            try:
                rows = await self._customer_metadata(cid)
            except (PermanentError, TransientError):
                rows = []
            if not rows:
                # Metadata unavailable → surface the accessible customer as-is.
                rows = [AdsCustomer(customer_id=cid)]
            for row in rows:
                if row.customer_id and row.customer_id not in emitted:
                    emitted.add(row.customer_id)
                    out.append(row)
        return out

    async def search(self, query: str, *, customer_id: str | None = None) -> list[dict[str, Any]]:
        """Run a GAQL query via `googleAds:searchStream` (all pages in one call).

        Used to resolve real language/geo target constants instead of hardcoding
        invented ids.
        """
        target = (customer_id or self._login_customer_id or "").replace("customers/", "")
        if not target:
            raise PermanentError("google_ads: no customer id available for a GAQL search")
        data = await self._request(
            "POST",
            f"/customers/{target}/googleAds:searchStream",
            json={"query": query},
            customer_id=target,
        )
        if isinstance(data, list):
            return [row for chunk in data for row in (chunk.get("results") or [])]
        return list((data or {}).get("results") or [])

    async def _customer_metadata(self, customer_id: str) -> list[AdsCustomer]:
        customer_id = customer_id.replace("customers/", "")
        query = (
            "SELECT customer_client.id, customer_client.descriptive_name, "
            "customer_client.currency_code, customer_client.time_zone, "
            "customer_client.manager, customer_client.status "
            "FROM customer_client WHERE customer_client.level <= 1"
        )
        rows = await self.search(query, customer_id=customer_id)
        out: list[AdsCustomer] = []
        for row in rows:
            cc = row.get("customerClient") or {}
            cid = str(cc.get("id") or "")
            if not cid:
                continue
            out.append(
                AdsCustomer(
                    customer_id=cid,
                    descriptive_name=str(cc.get("descriptiveName") or ""),
                    currency_code=str(cc.get("currencyCode") or ""),
                    time_zone=str(cc.get("timeZone") or ""),
                    manager_customer_id=customer_id,
                    is_manager=bool(cc.get("manager")),
                    status=str(cc.get("status") or ""),
                )
            )
        return out

    # -- keyword ideas ---------------------------------------------------------
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
    ) -> list[KeywordIdea]:
        """Page through GenerateKeywordIdeas, de-duplicating by idea text."""
        cid = customer_id.replace("customers/", "")
        body: dict[str, Any] = {
            "language": language,
            "geoTargetConstants": geo_targets or [],
            "keywordPlanNetwork": network,
            "includeAdultKeywords": include_adult_keywords,
            "pageSize": max(1, min(page_size, 10_000)),
        }
        kw = [k for k in (keywords or []) if k.strip()]
        if seed_kind == "url" and url:
            body["urlSeed"] = {"url": url}
        elif seed_kind == "site" and url:
            body["siteSeed"] = {"site": url}
        elif seed_kind == "keyword_and_url" and url and kw:
            body["keywordAndUrlSeed"] = {"url": url, "keywords": kw}
        elif kw:
            body["keywordSeed"] = {"keywords": kw}
        else:
            raise PermanentError("keyword research needs at least one seed keyword or URL")

        ideas: dict[str, KeywordIdea] = {}
        page_token = ""
        for _ in range(200):  # hard page cap: bounded memory + bounded cost
            if page_token:
                body["pageToken"] = page_token
            data = await self._request(
                "POST",
                f"/customers/{cid}:generateKeywordIdeas",
                json=body,
                customer_id=cid,
            )
            for row in data.get("results") or []:
                idea = self._parse_idea(row)
                if idea is None:
                    continue
                existing = ideas.get(idea.text)
                if existing is None:
                    ideas[idea.text] = idea
                else:
                    # same idea across seed pages: keep the larger volume
                    if idea.avg_monthly_searches > existing.avg_monthly_searches:
                        ideas[idea.text] = idea
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                break
            if max_results and len(ideas) >= max_results:
                break
        values = list(ideas.values())
        if max_results:
            values.sort(key=lambda i: i.avg_monthly_searches, reverse=True)
            values = values[:max_results]
        return values

    @staticmethod
    def _parse_idea(row: dict[str, Any]) -> KeywordIdea | None:
        text = str(row.get("text") or "").strip()
        if not text:
            return None
        metrics = row.get("keywordIdeaMetrics") or {}
        volumes: list[tuple[int, int, int]] = []
        for item in metrics.get("monthlySearchVolumes") or []:
            try:
                year = int(item.get("year") or 0)
                month = _month_number(item.get("month"))
                count = int(item.get("monthlySearches") or 0)
            except (TypeError, ValueError):
                continue
            if year and month:
                volumes.append((year, month, count))
        return KeywordIdea(
            text=text,
            avg_monthly_searches=int(metrics.get("avgMonthlySearches") or 0),
            competition=str(metrics.get("competition") or "UNSPECIFIED"),
            competition_index=int(metrics.get("competitionIndex") or 0),
            average_cpc_micros=int(metrics.get("averageCpcMicros") or 0),
            low_top_of_page_bid_micros=int(metrics.get("lowTopOfPageBidMicros") or 0),
            high_top_of_page_bid_micros=int(metrics.get("highTopOfPageBidMicros") or 0),
            monthly_volumes=volumes,
            source="google_ads",
        )

    async def ping(self) -> None:
        await self.list_accessible_customers()

    async def aclose(self) -> None:
        await self._http.aclose()


_MONTHS = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}


# Month values arrive either as an enum name ("JANUARY"), an int, or a bare
# enum number. All three are handled so a provider format change can't silently
# drop the historical volume series.
def _month_number(value: Any) -> int:
    if isinstance(value, int):
        return value if 1 <= value <= 12 else 0
    text = str(value or "").strip().upper()
    if not text:
        return 0
    if text in _MONTHS:
        return _MONTHS[text]
    # proto3 JSON may emit the numeric enum (JANUARY = 1 … DECEMBER = 12)
    try:
        number = int(text.rsplit("_", 1)[-1]) if "_" in text else int(text)
    except ValueError:
        return 0
    return number if 1 <= number <= 12 else 0


def _selfcheck() -> None:
    assert _month_number("JANUARY") == 1
    assert _month_number("DECEMBER") == 12
    assert _month_number(3) == 3
    assert _month_number("MONTH_OF_YEAR_UNSPECIFIED") == 0
    idea = GoogleAdsClient._parse_idea(
        {
            "text": "gym software",
            "keywordIdeaMetrics": {
                "avgMonthlySearches": 8100,
                "competition": "MEDIUM",
                "competitionIndex": 61,
                "averageCpcMicros": 4200000,
                "monthlySearchVolumes": [
                    {"year": 2026, "month": "AUGUST", "monthlySearches": 8200}
                ],
            },
        }
    )
    assert idea is not None and idea.avg_monthly_searches == 8100
    assert idea.competition == "MEDIUM" and idea.competition_index == 61
    assert idea.monthly_volumes == [(2026, 8, 8200)]
    assert GoogleAdsClient._parse_idea({"text": ""}) is None


if __name__ == "__main__":
    _selfcheck()
    print("google_ads selfcheck ok")
