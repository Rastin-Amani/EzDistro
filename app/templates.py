import os
from datetime import datetime

import jdatetime
from fastapi.templating import Jinja2Templates

from app.i18n import _, get_locale, locale_proxy, ngettext

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# i18n globals — `_`/`ngettext` read the request-scoped locale (contextvar,
# set by middleware); `locale` exposes metadata (code/direction/native_name).
templates.env.globals["_"] = _
templates.env.globals["ngettext"] = ngettext
templates.env.globals["locale"] = locale_proxy


# ---------------------------------------------------------------------------
# Locale-aware date helpers (fa = Jalali, others = Babel Gregorian)
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


def to_jalali_year(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return jdatetime.datetime.fromgregorian(datetime=parsed).year


def to_jalali_date(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return jdatetime.datetime.fromgregorian(datetime=parsed).strftime("%Y/%m/%d")


def to_jalali_datetime(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    return jdatetime.datetime.fromgregorian(datetime=parsed).strftime("%Y/%m/%d %H:%M")


def _babel_date(parsed, fmt):
    from babel.dates import format_date

    return format_date(parsed, format=fmt, locale=get_locale().code.replace("-", "_"))


def _babel_datetime(parsed, fmt):
    from babel.dates import format_datetime

    return format_datetime(parsed, format=fmt, locale=get_locale().code.replace("-", "_"))


def loc_year(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    if get_locale().code == "fa":
        return to_jalali_year(parsed)
    return _babel_date(parsed, "yyyy")


def loc_date(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    if get_locale().code == "fa":
        return to_jalali_date(parsed)
    return _babel_date(parsed, "medium")


def loc_datetime(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return date_str or ""
    if get_locale().code == "fa":
        return to_jalali_datetime(parsed)
    return _babel_datetime(parsed, "short")


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
}


def status_badge(status: str) -> str:
    return STATUS_STYLES.get(status, "badge-soft badge-neutral")


def status_label(status: str) -> str:
    labels = {
        "pending": _("Pending"),
        "retrying": _("Awaiting retry"),
        "running": _("Running"),
        "completed": _("Completed"),
        "failed": _("Failed"),
        "cancelled": _("Cancelled"),
        "planned": _("Planned"),
        "queued": _("Queued"),
        "planning": _("Outlining"),
        "outline_ready": _("Outline ready"),
        "writing": _("Writing"),
        "review": _("Review"),
        "approved": _("Approved"),
        "sent_back": _("Returned"),
        "publishing": _("Publishing"),
        "published": _("Published"),
        "skipped": _("Rejected"),
        "skipped_duplicate": _("Duplicate"),
        "draft": _("Draft"),
        "generating": _("Generating"),
        # images
        "optimizing": _("Optimizing"),
        "ready": _("Ready"),
        "uploading": _("Uploading"),
        "done": _("Done"),
        "indexed": _("Indexed"),
        "deleted": _("Deleted"),
        "manual": _("Manual"),
        "schedule": _("Schedule"),
        "healthy": _("Healthy"),
        "degraded": _("Degraded"),
        "unhealthy": _("Unhealthy"),
        "unknown": _("Unknown"),
        "info": _("Info"),
        "warning": _("Warning"),
        "error": _("Error"),
        "debug": _("Debug"),
        "job.created": _("Job created"),
        "job.claimed": _("Job claimed"),
        "job.started": _("Job started"),
        "job.stage_started": _("Stage started"),
        "job.stage_completed": _("Stage finished"),
        "job.retry_scheduled": _("Retry scheduled"),
        "job.provider_error": _("Provider error"),
        "job.completed": _("Job completed"),
        "job.failed": _("Job failed"),
        "job.cancelled": _("Job cancelled"),
        "provider_call": _("Provider call"),
    }
    return labels.get(status, status)


# ---------------------------------------------------------------------------
# Relative time (locale-aware)
# ---------------------------------------------------------------------------
def to_rel_time(date_str):
    parsed = _parse_dt(date_str)
    if not parsed:
        return "—"
    import datetime as dt

    seconds = max(0, int((dt.datetime.now(dt.UTC) - parsed.replace(tzinfo=dt.UTC)).total_seconds()))
    if seconds < 45:
        return _("just now")
    if seconds < 3600:
        n = seconds // 60
        return ngettext("1 minute ago", "%(n)d minutes ago", n) % {"n": n}
    if seconds < 86400:
        n = seconds // 3600
        return ngettext("1 hour ago", "%(n)d hours ago", n) % {"n": n}
    n = seconds // 86400
    return ngettext("1 day ago", "%(n)d days ago", n) % {"n": n}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
templates.env.filters["loc_year"] = loc_year
templates.env.filters["loc_date"] = loc_date
templates.env.filters["loc_dt"] = loc_datetime
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
