import os
import time
from datetime import datetime

from babel.dates import format_date, format_datetime
from babel.numbers import format_decimal
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(BASE_DIR, "static")

# asset_version() memo: (monotonic_timestamp, value). See the function docstring.
_ASSET_VERSION_TTL = 10.0
_asset_version_cache: tuple[float, str] = (0.0, "")


def _hx_partial(request):
    """`partial` is true for HTMX requests, so the shell layout renders only the
    swapped page body instead of the whole application chrome."""
    return {"partial": request.headers.get("HX-Request") == "true"}


def _has_projects(request):
    """Does the current user have any project at all?

    Drives the sidebar's "Start here → New project" hint, which only makes
    sense before the first project. Skipped for HTMX requests (those render a
    page fragment, never the shell) and returns nothing when the request has
    no PocketBase client, so the template can tell "unknown" from "zero".
    """
    if request.headers.get("HX-Request") == "true":
        return {}
    pb = getattr(request.state, "pb", None)
    user = getattr(request.state, "user", None)
    if pb is None or not user:
        return {}

    from app.repositories.members import MemberRepo
    from app.repositories.projects import ProjectRepo

    try:
        if user.get("role") == "admin":
            total = ProjectRepo(pb).count()
        else:
            total = MemberRepo(pb).count(filter=f'user="{user.get("id", "")}"')
    except Exception:  # noqa: BLE001 - a sidebar hint must never break a page
        return {}
    return {"has_projects": total > 0}


templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates"),
    context_processors=[_hx_partial, _has_projects],
)


# ---------------------------------------------------------------------------
# Date helpers (English-only Gregorian via Babel)
# ---------------------------------------------------------------------------
def _parse_dt(date_str):
    """Normalize PB timestamps (str or datetime) to a naive UTC datetime."""
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        value = date_str
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value
    try:
        clean = str(date_str).replace("Z", "").strip()
        clean = clean.split("+")[0]
        if "." in clean:
            return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S.%f")
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _babel_date(parsed, fmt):
    return format_date(parsed, format=fmt, locale="en")


def _babel_datetime(parsed, fmt):
    return format_datetime(parsed, format=fmt, locale="en")


def loc_year(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return _babel_date(parsed, "yyyy")


def loc_date(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return _babel_date(parsed, "medium")


def loc_datetime(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return _babel_datetime(parsed, "short")


def loc_int(value):
    """Group thousands for every raw count (search volume, SERP results…).

    Facts stay facts — this only makes them legible; never round or abbreviate.
    """
    if value is None or value == "":
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    return format_decimal(number, format="#,##0", locale="en")


def money_cents(value):
    """Format an integer minor-unit price (cents) as e.g. ``$9.00``."""
    if value is None or value == "":
        return "—"
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return value
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100}.{abs(cents) % 100:02d}"


def money_usd(value):
    """Format a float USD amount as e.g. ``$9.00``."""
    if value is None or value == "":
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return value
    return f"${amount:,.2f}"


# ---------------------------------------------------------------------------
# Status styling
# ---------------------------------------------------------------------------
STATUS_STYLES = {
    # jobs
    "pending": "badge-soft badge-info",
    "retrying": "badge-soft badge-warning",
    "running": "badge-soft badge-warning",
    "completed": "badge-soft badge-success",
    "failed": "badge-soft badge-error",
    "cancelled": "badge-soft badge-neutral",
    # topics
    "planned": "badge-soft badge-neutral",
    "queued": "badge-soft badge-info",
    "planning": "badge-soft badge-warning",
    "outline_ready": "badge-soft badge-info",
    "writing": "badge-soft badge-warning",
    "review": "badge-soft badge-warning",
    "approved": "badge-soft badge-success",
    "sent_back": "badge-soft badge-warning",
    "publishing": "badge-soft badge-warning",
    "published": "badge-soft badge-success",
    "skipped": "badge-soft badge-neutral",
    # articles / sections
    "draft": "badge-soft badge-neutral",
    "generating": "badge-soft badge-warning",
    "optimizing": "badge-soft badge-warning",
    "ready": "badge-soft badge-success",
    "uploading": "badge-soft badge-warning",
    "done": "badge-soft badge-success",
    "indexed": "badge-soft badge-success",
    "deleted": "badge-soft badge-neutral",
    "skipped_duplicate": "badge-soft badge-neutral",
    # runs / triggers / health
    "manual": "badge-soft badge-info",
    "schedule": "badge-soft badge-warning",
    "healthy": "badge-soft badge-success",
    "degraded": "badge-soft badge-warning",
    "unhealthy": "badge-soft badge-error",
    "unknown": "badge-soft badge-neutral",
    "info": "badge-soft badge-info",
    "warning": "badge-soft badge-warning",
    "error": "badge-soft badge-error",
    "debug": "badge-soft badge-neutral",
    "job.created": "badge-soft badge-info",
    "job.claimed": "badge-soft badge-info",
    "job.started": "badge-soft badge-info",
    "job.stage_started": "badge-soft badge-info",
    "job.stage_completed": "badge-soft badge-success",
    "job.retry_scheduled": "badge-soft badge-warning",
    "job.provider_error": "badge-soft badge-error",
    "job.completed": "badge-soft badge-success",
    "job.failed": "badge-soft badge-error",
    "job.cancelled": "badge-soft badge-neutral",
    "provider_call": "badge-soft badge-info",
    # billing
    "awaiting_payment": "badge-soft badge-info",
    "partially_paid": "badge-soft badge-warning",
    "paid": "badge-soft badge-success",
    "refunded": "badge-soft badge-neutral",
    "expired": "badge-soft badge-neutral",
    "needs_review": "badge-soft badge-warning",
}


def status_badge(status: str) -> str:
    return STATUS_STYLES.get(status, "badge-soft badge-neutral")


def status_label(status: str) -> str:
    labels = {
        "pending": ("Pending"),
        "retrying": ("Awaiting retry"),
        "running": ("Running"),
        "completed": ("Completed"),
        "failed": ("Failed"),
        "cancelled": ("Cancelled"),
        "planned": ("Planned"),
        "queued": ("Queued"),
        "planning": ("Outlining"),
        "outline_ready": ("Outline ready"),
        "writing": ("Writing"),
        "review": ("Review"),
        "approved": ("Approved"),
        "sent_back": ("Returned"),
        "publishing": ("Publishing"),
        "published": ("Published"),
        "skipped": ("Rejected"),
        "skipped_duplicate": ("Duplicate"),
        "draft": ("Draft"),
        "generating": ("Generating"),
        # images
        "optimizing": ("Optimizing"),
        "ready": ("Ready"),
        "uploading": ("Uploading"),
        "done": ("Done"),
        "indexed": ("Indexed"),
        "deleted": ("Deleted"),
        "manual": ("Manual"),
        "schedule": ("Schedule"),
        "healthy": ("Healthy"),
        "degraded": ("Degraded"),
        "unhealthy": ("Unhealthy"),
        "unknown": ("Unknown"),
        "info": ("Info"),
        "warning": ("Warning"),
        "error": ("Error"),
        "debug": ("Debug"),
        "job.created": ("Job created"),
        "job.claimed": ("Job claimed"),
        "job.started": ("Job started"),
        "job.stage_started": ("Stage started"),
        "job.stage_completed": ("Stage finished"),
        "job.retry_scheduled": ("Retry scheduled"),
        "job.provider_error": ("Provider error"),
        "job.completed": ("Job completed"),
        "job.failed": ("Job failed"),
        "job.cancelled": ("Job cancelled"),
        "provider_call": ("Provider call"),
        # billing
        "awaiting_payment": ("Awaiting payment"),
        "partially_paid": ("Partially paid"),
        "paid": ("Paid"),
        "refunded": ("Refunded"),
        "expired": ("Expired"),
        "needs_review": ("Needs review"),
    }
    return labels.get(status, status)


# ---------------------------------------------------------------------------
# Relative time (English-only)
# ---------------------------------------------------------------------------
def to_rel_time(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return "—"
    import datetime as dt

    seconds = max(0, int((dt.datetime.now(dt.UTC) - parsed.replace(tzinfo=dt.UTC)).total_seconds()))
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        n = seconds // 60
        return "1 minute ago" if n == 1 else f"{n} minutes ago"
    if seconds < 86400:
        n = seconds // 3600
        return "1 hour ago" if n == 1 else f"{n} hours ago"
    n = seconds // 86400
    return "1 day ago" if n == 1 else f"{n} days ago"


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
templates.env.filters["loc_year"] = loc_year
templates.env.filters["loc_date"] = loc_date
templates.env.filters["loc_dt"] = loc_datetime
templates.env.filters["loc_int"] = loc_int
templates.env.filters["money_cents"] = money_cents
templates.env.filters["money_usd"] = money_usd
templates.env.filters["status_badge"] = status_badge
templates.env.filters["status_label"] = status_label
templates.env.filters["rel_time"] = to_rel_time


def pb_file(record_id: object, filename: object) -> str:
    """PocketBase file URL for article_images binaries.

    File fields are public/unlisted (protected=False) — the admin UI points
    <img> tags straight at PocketBase; no proxy route, same security posture.
    """
    rid = str(record_id or "")
    name = str(filename or "")
    if not rid or not name:
        return ""
    from app.config import settings

    return f"{settings.pb_url.rstrip('/')}/api/files/article_images/{rid}/{name}"


templates.env.filters["pb_file"] = pb_file


def asset_version() -> str:
    """Cache key for /static css+js that follows the files themselves.

    `app_version` only moves on a release, so a rebuilt app.css kept being
    served from a browser cache under an unchanged `?v=` URL — new markup
    rendered against the old stylesheet. Derive the key from the newest asset
    instead, and recompute per render so rebuilding the CSS needs no restart.

    Memoized for a few seconds: base.html asks for this six times per page
    (one CSS + five script tags), and each call walked + stat'ed the whole
    static tree — six directory walks per page render for one value.
    """
    global _asset_version_cache
    now = time.monotonic()
    if now - _asset_version_cache[0] < _ASSET_VERSION_TTL:
        return _asset_version_cache[1]
    from pathlib import Path

    from app.config import settings

    newest = max(
        (p.stat().st_mtime for p in Path(_STATIC_DIR).rglob("*") if p.suffix in {".css", ".js"}),
        default=0.0,
    )
    value = f"{settings.app_version}-{int(newest)}"
    _asset_version_cache = (now, value)
    return value


templates.env.globals["asset_version"] = asset_version


def warm_templates() -> int:
    """Compile every template once, at boot.

    Jinja compiles lazily, so the FIRST request for each page paid its
    compilation (~150-400 ms) on the request path. In production also drop the
    per-render mtime check (`auto_reload=False`) — templates only change on a
    deploy there, while dev keeps auto_reload for live edits.
    """
    from pathlib import Path

    from app.config import settings

    templates.env.auto_reload = not settings.is_prod
    root = Path(os.path.join(BASE_DIR, "templates"))
    count = 0
    for path in root.rglob("*.html"):
        templates.env.get_template(str(path.relative_to(root)))
        count += 1
    return count
