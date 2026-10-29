"""IndexNow push (Bing, Yandex, Naver, Seznam, Yep) + the Google reality check.

IndexNow is open and shared: one POST of up to 10 000 URLs to ONE participating
endpoint is distributed to every engine on the participant list, so there is no
per-engine submission to manage.

Google is NOT a participant and has never adopted the protocol. Nothing here
pretends otherwise: for Google the UI offers the Search Console "Request
indexing" deep link (`gsc_inspect_link`), which is the only per-URL path Google
provides for ordinary content — its Indexing API is documented for job postings
and livestream pages only, so EzDistro does not call it for articles.

The key is public by design: engines fetch ``https://<host>/<key>.txt`` to
confirm ownership before they act on a submission. It therefore lives in the
integration's NON-secret `configuration` (never `secretsEnc`) — a key that is
served to the whole internet gains nothing from Fernet, and hiding it would
make the "copy this file to your site root" step impossible.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from app.domain.validation import UnsafeUrlError, validate_url
from app.providers.base import PermanentError, TransientError

logger = logging.getLogger(__name__)

# One participating endpoint is enough — submissions are shared between engines.
DEFAULT_ENDPOINT = "https://api.indexnow.org/indexnow"
# IndexNow caps a single POST at 10 000 URLs.
MAX_URLS = 10_000
# Keys are 8-128 chars of [A-Za-z0-9-,_]; hex keeps them copy-paste safe.
KEY_BYTES = 16


def generate_key() -> str:
    return _secrets.token_hex(KEY_BYTES)


@dataclass
class IndexNowResult:
    """What the endpoint replied. A 200/202 is a RECEIPT, never "indexed"."""

    status_code: int
    accepted: bool
    message: str
    endpoint: str = ""
    host: str = ""
    url_count: int = 0


def url_host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def site_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _property_name(value: str) -> str:
    """A Search Console property name from a URL or a bare domain.

    `sc-domain:example.com` is kept verbatim (Domain property); anything with a
    scheme is reduced to its host; a bare host is used as-is.
    """
    text = (value or "").strip()
    if not text or text.startswith("sc-domain:"):
        return text
    if "://" in text:
        return url_host(text)
    return text.strip("/")


def gsc_inspect_link(page_url: str, site_url: str = "") -> str:
    """Search Console URL Inspection, opened on this page.

    `resource_id` must be the property as Search Console names it — a bare
    domain ("example.com") or "sc-domain:example.com" for a Domain property.
    An `https://…/` value makes Search Console fall back to the property picker
    instead of the page, so the scheme is deliberately never sent.
    """
    resource = _property_name(site_url) or url_host(page_url)
    return "https://search.google.com/search-console/inspect?" + urlencode(
        {"resource_id": resource, "url": page_url, "hl": "en"}
    )


class IndexNowClient:
    """Submits URLs to one IndexNow endpoint and verifies the key file."""

    category = "indexing"
    provider_name = "indexnow"
    PROVIDER_META: dict[str, Any] = {
        "description": (
            "IndexNow push — notifies Bing, Yandex, Naver, Seznam and Yep. "
            "Google is not on IndexNow; it gets a Search Console link instead."
        ),
        "requires_api_key": False,
        "supports_model_listing": False,
    }

    def __init__(
        self,
        *,
        host: str = "",
        key: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        key_location: str = "",
        attempts: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self.host = (host or "").strip().lower()
        self.key = (key or "").strip()
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).strip()
        self.key_location = (key_location or "").strip()
        self.attempts = max(1, attempts)
        self.timeout = timeout
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PermanentError(f"IndexNow endpoint is not an http(s) URL: {self.endpoint!r}")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._path = parsed.path or "/indexnow"
        self._client: Any | None = None

    # -- key file --------------------------------------------------------------
    @property
    def key_url(self) -> str:
        """Where engines look for the key (the configured keyLocation wins)."""
        if self.key_location:
            return self.key_location
        if not self.host or not self.key:
            return ""
        return f"https://{self.host}/{self.key}.txt"

    def _require_key(self) -> None:
        if not self.key:
            raise PermanentError(
                "IndexNow has no key for this website — generate one on the Index Now tab"
            )
        if not (8 <= len(self.key) <= 128):
            raise PermanentError("IndexNow keys must be 8-128 characters")

    async def verify_key(self) -> str:
        """Fetch the key file; raise unless the site serves exactly this key.

        This is THE failure mode of IndexNow: without a reachable key file every
        submission is accepted (202) and then silently discarded.
        """
        self._require_key()
        url = self.key_url
        if not url:
            raise PermanentError("IndexNow needs the site host before it can check the key file")
        try:
            checked = validate_url(url)
        except UnsafeUrlError as exc:
            raise PermanentError(f"unsafe site URL for the key file: {exc}") from exc
        client = await self._http()
        response = await client.get(checked, headers={"Accept": "text/plain"})
        status = response.status_code
        if status in (429,) or 500 <= status <= 599:
            raise TransientError(f"key file check got HTTP {status}", {"status": status})
        if not response.is_success:
            raise PermanentError(
                f"{url} returned HTTP {status}. Upload the key file to the site root "
                "and make sure it stays reachable (not blocked by auth, CDN rules or "
                "a WordPress .htaccess).",
                {"status": status, "url": url},
            )
        body = response.text.strip().strip("\ufeff")
        if body != self.key:
            raise PermanentError(
                f"{url} does not serve this key (it returned {len(body)} characters). "
                "The file must contain the key and nothing else — regenerate the key "
                "and re-upload if the site was set up with a different one.",
                {"url": url},
            )
        return url

    # -- submission ------------------------------------------------------------
    async def submit(self, urls: Sequence[str]) -> IndexNowResult:
        self._require_key()
        clean = [str(u).strip() for u in urls if str(u).strip()]
        if not clean:
            raise PermanentError("no URLs to submit")
        if len(clean) > MAX_URLS:
            raise PermanentError(
                f"IndexNow takes at most {MAX_URLS} URLs per submission (got {len(clean)}) — "
                "submit them in batches"
            )
        # The URL host is the truth. IndexNow verifies the key file ON the host it
        # receives and then requires every submitted URL to belong to it, so a
        # mismatch between the saved host, the URLs and the key file is rejected
        # by the engine with an opaque "InvalidRequestParameters".
        hosts = {url_host(u) for u in clean}
        if "" in hosts:
            raise PermanentError(f"could not read a host from: {clean[0]}")
        if len(hosts) > 1:
            raise PermanentError(
                "one submission cannot mix hosts ("
                + ", ".join(sorted(hosts))
                + ") — submit each site separately"
            )
        host = next(iter(hosts))
        if self.host and host != self.host:
            raise PermanentError(
                f"these URLs are on {host}, but this website's host is set to {self.host}. "
                f"IndexNow checks the key file on the host it receives, so both must be the "
                f"same — set the website host to {host} on the Index Now tab."
            )
        if self.key_location and url_host(self.key_location) != host:
            raise PermanentError(
                f"the key file is served from {url_host(self.key_location)}, but these URLs "
                f"are on {host}. IndexNow verifies the key on the submitted host, so the file "
                f"must live on {host} too — reinstall it (Index Now → Install on WordPress) "
                "or fix the key file location."
            )
        for url in clean:
            if urlparse(url).scheme not in ("http", "https"):
                raise PermanentError(f"url is not http(s): {url}")
        try:
            validate_url(f"https://{host}")
        except UnsafeUrlError as exc:
            raise PermanentError(f"unsafe submission host {host}: {exc}") from exc

        payload: dict[str, Any] = {"host": host, "key": self.key, "urlList": clean}
        if self.key_location:
            payload["keyLocation"] = self.key_location

        from app.providers.http import with_retry

        async def call() -> Any:
            client = await self._http()
            response = await client.post(
                self._path,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            return self._classify(response, host, len(clean))

        return await with_retry(call, attempts=self.attempts, what="indexnow submit")

    def _classify(self, response: Any, host: str, count: int) -> IndexNowResult:
        status = int(response.status_code)
        body = (response.text or "")[:300].strip()
        if status in (200, 202):
            return IndexNowResult(
                status_code=status,
                accepted=True,
                # 202 is the normal reply: received, key validation happens
                # afterwards — a whole batch is dropped when the key file is
                # missing, which is why the Index Now tab verifies it up front.
                message=body or "accepted — the engines will fetch and verify the key file",
                endpoint=self.endpoint,
                host=host,
                url_count=count,
            )
        # The engine's body is JSON ({"errorCode", "message"}); show a human
        # sentence first and keep the provider's own words in the details.
        error_code = ""
        provider_message = body
        if body.startswith("{"):
            try:
                data = json.loads(body)
                error_code = str(data.get("errorCode") or "")
                provider_message = str(data.get("message") or "")
            except (ValueError, TypeError):
                pass
        message = _HUMAN.get(status) or provider_message or f"HTTP {status}"
        if provider_message and provider_message not in message:
            message = f"{message} — {provider_message}"
        if error_code:
            message = f"{message} ({error_code})"
        details: dict[str, Any] = {"status": status, "host": host, "endpoint": self.endpoint}
        if error_code:
            details["errorCode"] = error_code
        if provider_message:
            details["providerMessage"] = provider_message
        if self.key_location:
            details["keyLocation"] = self.key_location
        if status == 429 or 500 <= status <= 599:
            raise TransientError(f"IndexNow: {message}", details)
        raise PermanentError(f"IndexNow: {message}", details)

    # -- plumbing --------------------------------------------------------------
    async def _http(self) -> Any:
        if self._client is None:
            from app.providers.http import acquire_async_client

            self._client = acquire_async_client(self._origin, timeout=self.timeout)
        return self._client

    async def ping(self) -> None:
        await self.verify_key()

    async def aclose(self) -> None:
        if self._client is not None:
            from app.providers.http import release_async_client

            release_async_client(self._client)
            self._client = None


_HUMAN = {
    400: "the request was malformed — check the key and the URL list",
    401: "the endpoint refused the request (unauthorized)",
    403: (
        "the key was rejected — the <key>.txt file is missing from the site root or "
        "does not contain this exact key, so the batch will be dropped"
    ),
    422: (
        "the engine did not accept these URLs as belonging to the site it verified for "
        "this key — the key file and the URLs must be on the SAME host (www.example.com "
        "is a different host from example.com)"
    ),
    429: "too many requests — EZDistro will retry automatically",
}


def gsc_site_url(inspect_link: str) -> str:
    """Reverse the property out of a built inspect link (used by tests/UI hints)."""
    query = dict(parse_qsl(urlparse(inspect_link).query))
    return str(query.get("resource_id") or "")


def _selfcheck() -> None:
    key = generate_key()
    assert 8 <= len(key) <= 128 and key.isalnum()

    client = IndexNowClient(host="example.com", key=key)
    assert client.key_url == f"https://example.com/{key}.txt"
    assert client._origin == "https://api.indexnow.org" and client._path == "/indexnow"

    url = "https://example.com/blog/post?a=1"
    link = gsc_inspect_link(url)
    assert gsc_site_url(link) == "example.com"  # bare domain, never https://
    assert "resource_id=example.com" in link
    assert "url=https%3A%2F%2Fexample.com%2Fblog%2Fpost%3Fa%3D1" in link
    # a URL-prefix property is reduced to the same bare domain
    assert gsc_site_url(gsc_inspect_link(url, "https://example.com/blog/")) == "example.com"
    # a Domain property is passed through verbatim
    assert gsc_site_url(gsc_inspect_link(url, "sc-domain:example.com")) == "sc-domain:example.com"
    assert url_host("https://WWW.Example.com/x") == "www.example.com"
    assert site_origin("http://s.test:8080/a/b") == "http://s.test:8080"

    # a foreign host must never ride along in someone else's batch
    try:
        import asyncio

        asyncio.run(IndexNowClient(host="example.com", key=key).submit([url, "https://evil.io/x"]))
    except PermanentError as exc:
        assert "cannot mix hosts" in str(exc)
    else:
        raise AssertionError("foreign url was accepted")

    # a key file on another host is the exact cause of the engine's
    # "URLs are not related to your site verified" rejection
    try:
        import asyncio

        asyncio.run(
            IndexNowClient(key=key, key_location="https://cdn.example.net/key.txt").submit([url])
        )
    except PermanentError as exc:
        assert "served from cdn.example.net" in str(exc)
        assert "are on example.com" in str(exc)
    else:
        raise AssertionError("a key file on another host was accepted")

    class Resp:
        status_code = 422
        text = (
            '{"errorCode":"InvalidRequestParameters","message":"One or more URLs are '
            'not related to your site verified."}'
        )

    try:
        client._classify(Resp(), "example.com", 1)  # type: ignore[arg-type]
    except PermanentError as exc:
        # human sentence first, provider detail kept, error code visible
        assert "SAME host" in str(exc)
        assert "InvalidRequestParameters" in str(exc)
        assert "not related to your site" in str(exc)
    else:
        raise AssertionError("422 was accepted")

    class Forbidden:
        status_code = 403
        text = ""

    try:
        client._classify(Forbidden(), "example.com", 1)  # type: ignore[arg-type]
    except PermanentError as exc:
        assert "key was rejected" in str(exc)
    else:
        raise AssertionError("403 was accepted")

    class Ok:
        status_code = 202
        text = ""

    result = client._classify(Ok(), "example.com", 1)  # type: ignore[arg-type]
    assert result.accepted and result.url_count == 1 and result.status_code == 202
    client.key = "short"
    try:
        import asyncio

        asyncio.run(client.submit([url]))
    except PermanentError:
        pass
    else:
        raise AssertionError("a too-short key was accepted")


if __name__ == "__main__":
    _selfcheck()
    print("indexnow selfcheck ok")
