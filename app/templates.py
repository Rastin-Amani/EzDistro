import os
from datetime import datetime

import jdatetime
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ---------------------------------------------------------------------------
# Date helpers (Jalali)
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
        "pending": "در انتظار",
        "retrying": "در انتظار تلاش مجدد",
        "running": "در حال اجرا",
        "completed": "تکمیل شده",
        "failed": "ناموفق",
        "cancelled": "لغو شده",
        "planned": "برنامه‌ریزی‌شده",
        "queued": "در صف",
        "planning": "در حال رئوس‌سازی",
        "outline_ready": "رئوس آماده",
        "writing": "در حال نگارش",
        "review": "بازبینی",
        "approved": "تأیید شده",
        "sent_back": "بازگردانده شده",
        "publishing": "در حال انتشار",
        "published": "منتشر شده",
        "skipped": "رد شده",
        "skipped_duplicate": "تکراری",
        "draft": "پیش‌نویس",
        "generating": "در حال تولید",
        "done": "انجام شد",
        "indexed": "نمایه شده",
        "deleted": "حذف شده",
        "manual": "دستی",
        "schedule": "زمان‌بندی",
        "healthy": "سالم",
        "degraded": "مشکل‌دار",
        "unhealthy": "ناسالم",
        "unknown": "نامشخص",
        "info": "اطلاعات",
        "warning": "هشدار",
        "error": "خطا",
        "debug": "دیباگ",
        "job.created": "ایجاد وظیفه",
        "job.claimed": "دریافت وظیفه",
        "job.started": "شروع وظیفه",
        "job.stage_started": "شروع مرحله",
        "job.stage_completed": "پایان مرحله",
        "job.retry_scheduled": "زمان‌بندی تلاش مجدد",
        "job.provider_error": "خطای ارائه‌دهنده",
        "job.completed": "تکمیل وظیفه",
        "job.failed": "شکست وظیفه",
        "job.cancelled": "لغو وظیفه",
        "provider_call": "فراخوانی ارائه‌دهنده",
    }
    return labels.get(status, status)


# ---------------------------------------------------------------------------
# Relative time (Persian)
# ---------------------------------------------------------------------------
def to_rel_time(date_str):
    """'همین حالا' / '۳ دقیقه پیش' / '۲ ساعت پیش' / '۵ روز پیش'."""
    import datetime as dt

    parsed = _parse_dt(date_str)
    if not parsed:
        return "—"
    seconds = max(0, int((dt.datetime.now(dt.UTC) - parsed.replace(tzinfo=dt.UTC)).total_seconds()))
    if seconds < 45:
        return "همین حالا"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
templates.env.filters["jalali_year"] = to_jalali_year
templates.env.filters["jalali_date"] = to_jalali_date
templates.env.filters["jalali_dt"] = to_jalali_datetime
templates.env.filters["status_badge"] = status_badge
templates.env.filters["status_label"] = status_label
templates.env.filters["rel_time"] = to_rel_time
