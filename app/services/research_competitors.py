"""Competitor content intelligence without a paid competitor database.

Sitemap-first and robots-respecting: `/robots.txt` → declared sitemaps → a
(tiny) sitemap index walk → canonical, content-shaped URLs. Only when a domain
publishes no usable sitemap does it fall back to one level of internal links
from the homepage. No browser is involved — these are ordinary HTML pages.

Caching: a page is re-fetched only when its stored copy is older than
`COMPETITOR_TTL_SECONDS`; unchanged text is recorded as `unchanged` and never
re-analysed. On-page observations are stored as *observed* data — nothing here
claims to be a Google ranking factor.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from pocketbase.errors import ClientResponseError

from app.domain.chunker import strip_html
from app.domain.validation import validate_url
from app.jobs.context import JobContext
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.research import CompetitorPageRepo

USER_AGENT = "EZDistroBot/0.1 (+https://ezdistro.space/bot)"
MAX_HTML_BYTES = 2_000_000
SITEMAP_FILES_PER_DOMAIN = 50
SITEMAP_URLS_PER_DOMAIN = 20_000
COMPETITOR_TTL_SECONDS = 30 * 86_400
HEADINGS_PER_LEVEL = 40

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "msclkid", "ref", "source")
_SKIP_PATTERNS = (
    "/wp-admin",
    "/wp-login",
    "/login",
    "/signin",
    "/signup",
    "/register",
    "/cart",
    "/checkout",
    "/account",
    "/my-account",
    "/search",
    "/feed",
    "/tag/",
    "/author/",
    "/page/",
    "/privacy",
    "/terms",
    "/cookie",
    "/404",
)
_SKIP_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".pdf",
    ".zip",
    ".css",
    ".js",
    ".mp4",
    ".ico",
    ".xml",
    ".json",
)


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------
class _PageParser(HTMLParser):
    """Minimal, dependency-free page parser (title/meta/canonical/headings/JSON-LD)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.canonical = ""
        self.lang = ""
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.links: list[str] = []
        self.images = 0
        self.ld_json: list[str] = []
        self._capture = ""
        self._buf: list[str] = []
        self._depth = 0

    def _begin(self, what: str) -> None:
        self._capture = what
        self._buf = []
        self._depth = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        if self._capture == "title":
            self.title = text
        elif self._capture == "json":
            self.ld_json.append(text)
        elif self._capture in self.headings:
            self.headings[self._capture].append(text)
        self._capture = ""
        self._buf = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k: (v or "") for k, v in attrs}
        if tag == "a" and data.get("href"):
            self.links.append(data["href"])
        if self._capture:
            self._depth += 1
            return
        if tag == "html":
            self.lang = data.get("lang") or self.lang
        elif tag == "img":
            self.images += 1
        elif tag == "title":
            self._begin("title")
        elif tag == "script" and data.get("type", "").lower() == "application/ld+json":
            self._begin("json")
        elif tag == "meta":
            name = (data.get("name") or data.get("property") or "").lower()
            if name in ("description", "og:description") and not self.meta_description:
                self.meta_description = data.get("content", "").strip()
        elif tag == "link" and "canonical" in (data.get("rel") or "").lower():
            self.canonical = data.get("href", "").strip()
        elif tag in self.headings:
            self._begin(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        if tag == ("script" if self._capture == "json" else self._capture):
            self._flush()
        elif self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)


def canonicalize(url: str) -> str:
    """Normalize a URL for identity: scheme/host case, default ports, tracking, fragment."""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    netloc = host if parsed.port in (None, 80, 443) else f"{host}:{parsed.port}"
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = "&".join(
        part
        for part in (parsed.query or "").split("&")
        if part and not part.lower().startswith(_TRACKING_PARAMS)
    )
    return urlunparse((parsed.scheme or "https", netloc, path, "", query, ""))


def _schema_types(blocks: list[str]) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if len(found) >= 20:
            return
        if isinstance(node, dict):
            kind = node.get("@type")
            for value in kind if isinstance(kind, list) else [kind]:
                if isinstance(value, str) and value not in found:
                    found.append(value)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in blocks:
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            continue
    return found[:20]


def _content_type(url: str, headings: dict[str, list[str]], schema_types: list[str]) -> str:
    """Observed page shape from URL + structure. A heuristic, not a ranking claim."""
    path = urlparse(url).path.lower()
    if any(s in schema_types for s in ("FAQPage", "QAPage")) or "/faq" in path:
        return "faq"
    if any(s in path for s in ("/vs-", "/versus", "/comparison", "/compare")):
        return "comparison"
    if any(s in path for s in ("/pricing", "/plans", "/product", "/features/", "/solutions")):
        return "landing"
    if any(s in path for s in ("/docs", "/documentation", "/api-reference", "/help/")):
        return "documentation"
    if any(s in path for s in ("/blog/", "/article", "/news/", "/post/", "/insights")):
        return "article"
    if any(s in schema_types for s in ("Article", "BlogPosting", "NewsArticle")):
        return "article"
    if headings.get("h2"):
        return "page"
    return "page"


def extract_page(html_text: str, url: str) -> dict[str, Any]:
    """Observed on-page data for one fetched page."""
    parser = _PageParser()
    with contextlib.suppress(Exception):  # malformed markup must not abort a crawl
        parser.feed(html_text)
    text = strip_html(html_text)
    schema_types = _schema_types(parser.ld_json)
    headings = {level: values[:HEADINGS_PER_LEVEL] for level, values in parser.headings.items()}
    return {
        "title": parser.title[:1000],
        "metaDescription": parser.meta_description[:2000],
        "canonicalUrl": canonicalize(urljoin(url, parser.canonical or url)),
        "h1": (headings["h1"][0][:1000] if headings["h1"] else ""),
        "headings": headings,
        "wordCount": len(text.split()),
        "language": parser.lang[:20],
        "schemaTypes": schema_types,
        "contentType": _content_type(url, headings, schema_types),
        "links": [link for link in parser.links if link][:500],
        "images": parser.images,
        "text": text,
    }


# ---------------------------------------------------------------------------
# robots.txt + sitemaps
# ---------------------------------------------------------------------------
def parse_robots(text: str, *, user_agent: str = "*") -> dict[str, list[str]]:
    """Return {'sitemaps': [...], 'disallow': [...]} for the given UA block."""
    sitemaps: list[str] = []
    disallow: list[str] = []
    active = False
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key == "user-agent":
            active = value == "*" or user_agent.lower() in value.lower()
        elif key == "disallow" and active and value:
            disallow.append(value)
    return {"sitemaps": sitemaps, "disallow": disallow}


def parse_sitemap(text: str) -> tuple[list[str], list[str]]:
    """(page_urls, nested_sitemaps) from a sitemap or sitemap index.

    ponytail: regex <loc> extraction instead of an XML parser — sitemaps are
    flat documents and this dodges XML entity issues entirely.
    """
    locs = [loc.strip() for loc in _LOC_RE.findall(text or "")]
    nested = [loc for loc in locs if loc.lower().endswith(".xml") or "sitemap" in loc.lower()]
    pages = [loc for loc in locs if loc not in nested]
    return pages, nested


def robots_allowed(url: str, disallow: list[str]) -> bool:
    path = urlparse(url).path or "/"
    return not any(rule in path for rule in disallow if rule != "/")


def is_content_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(_SKIP_SUFFIXES):
        return False
    return not any(pattern in path for pattern in _SKIP_PATTERNS)


def _domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
async def _fetch_text(
    client: httpx.AsyncClient, url: str, *, verify_urls: bool = True
) -> str | None:
    if verify_urls:
        await asyncio.to_thread(validate_url, url)
    response = await client.get(url, headers={"User-Agent": USER_AGENT})
    if response.status_code >= 400:
        return None
    if len(response.content) > MAX_HTML_BYTES:
        return None
    return response.text


async def _discover_urls(
    client: httpx.AsyncClient, domain_url: str, *, verify_urls: bool
) -> tuple[list[str], list[str]]:
    """(content urls, disallow rules) for one domain — sitemap-first."""
    base = domain_url.rstrip("/")
    disallow: list[str] = []
    sitemaps: list[str] = []
    robots = await _fetch_text(client, f"{base}/robots.txt", verify_urls=verify_urls)
    if robots:
        parsed = parse_robots(robots)
        disallow = parsed["disallow"]
        sitemaps = [urljoin(base + "/", s) for s in parsed["sitemaps"]]
    if not sitemaps:
        sitemaps = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]

    urls: list[str] = []
    seen_maps: set[str] = set()
    for index, sitemap in enumerate(sitemaps):
        if len(urls) >= SITEMAP_URLS_PER_DOMAIN or index >= SITEMAP_FILES_PER_DOMAIN:
            break
        if sitemap in seen_maps:
            continue
        seen_maps.add(sitemap)
        body = await _fetch_text(client, sitemap, verify_urls=verify_urls)
        if not body:
            continue
        pages, nested = parse_sitemap(body)
        urls.extend(pages)
        for child in nested[:SITEMAP_FILES_PER_DOMAIN]:
            if len(urls) >= SITEMAP_URLS_PER_DOMAIN:
                break
            if child in seen_maps:
                continue
            seen_maps.add(child)
            child_body = await _fetch_text(client, child, verify_urls=verify_urls)
            if child_body:
                urls.extend(parse_sitemap(child_body)[0])

    canonical = []
    seen: set[str] = set()
    for url in urls:
        clean = canonicalize(url)
        if not clean or clean in seen or not is_content_url(clean):
            continue
        seen.add(clean)
        canonical.append(clean)

    if not canonical:  # no usable sitemap — one level of internal links
        home = await _fetch_text(client, base, verify_urls=verify_urls)
        if home:
            for link in extract_page(home, base)["links"]:
                clean = canonicalize(urljoin(base + "/", link))
                if (
                    clean
                    and clean not in seen
                    and _domain_of(clean) == _domain_of(base)
                    and is_content_url(clean)
                ):
                    seen.add(clean)
                    canonical.append(clean)
    return canonical[:SITEMAP_URLS_PER_DOMAIN], disallow


def _is_fresh(record: dict[str, Any]) -> bool:
    stamp = record.get("fetchedAt") or ""
    try:
        fetched = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=dt.UTC)
    return 0 <= (now_utc() - fetched).total_seconds() < COMPETITOR_TTL_SECONDS


async def _process_url(
    ctx: JobContext,
    client: httpx.AsyncClient,
    *,
    pages: CompetitorPageRepo,
    run_id: str,
    url: str,
    verify_urls: bool,
) -> str:
    """Fetch + store one competitor page. Returns fetched|unchanged|failed|skipped."""
    existing = pages.get_for_canonical(ctx.project_id, url)
    if existing and existing.get("status") == "fetched" and _is_fresh(existing):
        return "skipped"
    try:
        body = await _fetch_text(client, url, verify_urls=verify_urls)
    except Exception as exc:
        if existing:
            pages.update(existing["id"], {"status": "failed", "error": str(exc)[:2000]})
        return "failed"
    if body is None:
        if existing:
            pages.update(existing["id"], {"status": "skipped", "error": "not fetchable"})
        return "skipped"

    data = extract_page(body, url)
    content_hash = hashlib.sha256(data["text"].encode("utf-8")).hexdigest()
    text_hash = hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()
    payload: dict[str, Any] = {
        "project": ctx.project_id,
        "run": run_id,
        "domain": _domain_of(url),
        "url": url,
        "canonicalUrl": data["canonicalUrl"] or url,
        "title": data["title"],
        "metaDescription": data["metaDescription"],
        "h1": data["h1"],
        "headings": {level: values for level, values in data["headings"].items() if values},
        "wordCount": data["wordCount"],
        "contentType": data["contentType"],
        "schemaTypes": data["schemaTypes"],
        "language": data["language"],
        "contentHash": content_hash,
        "textHash": text_hash,
        "status": "fetched",
        "error": "",
        "fetchedAt": pb_dt(now_utc()),
    }
    if existing and existing.get("contentHash") and existing["contentHash"] != content_hash:
        payload["lastChangedAt"] = pb_dt(now_utc())
    # Unique index is (project, canonicalUrl); two requested URLs can resolve to
    # the same canonical, so look the record up by canonical before writing.
    canonical = payload["canonicalUrl"]
    stored = existing if canonical == url else pages.get_for_canonical(ctx.project_id, canonical)
    if stored:
        pages.update(stored["id"], payload)
        return "fetched" if stored.get("contentHash") != content_hash else "unchanged"
    try:
        pages.create(payload)
    except ClientResponseError as exc:
        if exc.status not in (400, 409):
            raise
        # A concurrent worker created this canonical first — fold into it.
        stored = pages.get_for_canonical(ctx.project_id, canonical)
        if stored is None:
            raise
        pages.update(stored["id"], payload)
    return "fetched"


async def crawl_competitors(
    ctx: JobContext,
    *,
    run_id: str,
    domains: list[str],
    max_pages: int = 200,
    transport: httpx.AsyncBaseTransport | None = None,
    verify_urls: bool = True,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Crawl competitor domains (sitemap-first) into `competitor_pages`."""
    targets = [d.strip() for d in domains if d and d.strip()]
    if not targets:
        return {"domains": 0, "pages": 0, "fetched": 0, "unchanged": 0, "skipped": 0, "failed": 0}

    pages = CompetitorPageRepo(ctx.pb)
    totals = {
        "domains": len(targets),
        "pages": 0,
        "fetched": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }
    budget = max(1, int(max_pages))
    limits = asyncio.Semaphore(max(1, int(concurrency)))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0), follow_redirects=True, transport=transport
    ) as client:
        for index, domain in enumerate(targets, start=1):
            await ctx.check_cancelled()
            if budget <= 0:
                break
            base = domain if "://" in domain else f"https://{domain}"
            ctx.progress(
                60 + int(10 * (index - 1) / len(targets)),
                stage="competitor_crawl",
                message=f"Discovering pages on {_domain_of(base)}…",
                current=index,
                total=len(targets),
            )
            try:
                urls, disallow = await _discover_urls(client, base, verify_urls=verify_urls)
            except Exception as exc:
                ctx.warning("competitor discovery failed", {"domain": base, "error": str(exc)})
                continue
            allowed = [u for u in urls if robots_allowed(u, disallow)][:budget]

            async def _run(url: str) -> str:
                async with limits:
                    return await _process_url(
                        ctx, client, pages=pages, run_id=run_id, url=url, verify_urls=verify_urls
                    )

            results = await asyncio.gather(*(_run(url) for url in allowed))
            budget -= len(allowed)
            totals["pages"] += len(allowed)
            for outcome in results:
                totals[outcome] = totals.get(outcome, 0) + 1
            ctx.info(
                "competitor domain crawled",
                {"domain": base, "discovered": len(urls), "crawled": len(allowed)},
            )

    return totals


def _demo() -> None:
    page = extract_page(
        "<html lang='en'><head><title>Gym CRM Comparison</title>"
        "<meta name='description' content='Compare gym CRMs'>"
        "<link rel='canonical' href='/vs/rival'>"
        '<script type=\'application/ld+json\'>{"@type": "FAQPage"}</script></head>'
        "<body><h1>Gym CRM comparison</h1><h2>Pricing</h2><img src='a.png'>"
        "<a href='/pricing'>Pricing</a></body></html>",
        "https://example.com/vs/rival",
    )
    assert page["title"] == "Gym CRM Comparison", page["title"]
    assert page["metaDescription"] == "Compare gym CRMs"
    assert page["canonicalUrl"] == "https://example.com/vs/rival", page["canonicalUrl"]
    assert page["h1"] == "Gym CRM comparison"
    assert page["headings"]["h2"] == ["Pricing"]
    assert page["schemaTypes"] == ["FAQPage"]
    assert page["contentType"] == "faq"
    assert page["images"] == 1

    assert canonicalize("HTTPS://Example.com:443/a/b/?utm_source=x&q=1#frag") == (
        "https://example.com/a/b?q=1"
    )
    assert canonicalize("not a url") == ""

    robots = parse_robots(
        "User-agent: *\nDisallow: /wp-admin\nSitemap: https://x.com/sitemap.xml\n"
    )
    assert robots["sitemaps"] == ["https://x.com/sitemap.xml"]
    assert robots["disallow"] == ["/wp-admin"]
    assert robots_allowed("https://x.com/blog/a", robots["disallow"])
    assert not robots_allowed("https://x.com/wp-admin/x", robots["disallow"])

    urls, nested = parse_sitemap(
        "<urlset><url><loc>https://x.com/a</loc></url>"
        "<url><loc>https://x.com/b</loc></url></urlset>"
    )
    assert urls == ["https://x.com/a", "https://x.com/b"] and nested == []
    assert parse_sitemap(
        "<sitemapindex><sitemap><loc>https://x.com/s2.xml</loc></sitemap></sitemapindex>"
    )[1] == ["https://x.com/s2.xml"]

    assert is_content_url("https://x.com/blog/post-name")
    assert not is_content_url("https://x.com/wp-admin/post.php")
    assert not is_content_url("https://x.com/logo.png")


if __name__ == "__main__":
    _demo()
    print("research_competitors ok")
