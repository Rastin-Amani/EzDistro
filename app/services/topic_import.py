"""Bulk / CSV import of topics.

Pure, testable parsing layer plus a thin PocketBase write path:

- ``parse_delimited``: auto-detects tab/comma/semicolon/pipe CSV (quote aware)
  and returns header-inclusive rows.
- ``detect_columns`` / ``build_column_map``: recognizes Persian/English header
  aliases (Title/عنوان, Related Pillar/ستون, …) so pasted Excel/CSV columns map
  without manual setup; every column can still be re-mapped by the user.
- ``import_rows``: creates topics, skipping rows whose normalized title or
  keyword already exists in the project (or earlier in the same batch), and
  reporting per-row errors instead of aborting.

The preview → import hand-off stores the parsed rows in a tiny in-process cache
keyed by a one-time token, so the CSV payload is transmitted once instead of
being re-posted on confirm.
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from typing import Any

from app.i18n import _
from app.repositories.topics import TOPIC_TYPES, TopicRepo

# --- column targets ---------------------------------------------------------
IMPORT_FIELDS = ("title", "keyword", "pillar", "cluster", "type", "priority", "week", "url")


def field_labels() -> dict[str, str]:
    """Lazy (request-time) translations for import-mapping UI labels."""
    return {
        "title": _("عنوان (الزامی)"),
        "keyword": _("کلمه کلیدی"),
        "pillar": _("ستون (Pillar)"),
        "cluster": _("خوشه (Cluster)"),
        "type": _("نوع"),
        "priority": _("اولویت"),
        "week": _("هفته"),
        "url": _("لینک"),
    }


HEADER_ALIASES: dict[str, set[str]] = {
    "title": {
        "title",
        "topic",
        "عنوان",
        "عنوان موضوع",
        "عنوان مقاله",
        "موضوع",
        "موضوع مقاله",
        "تیتر",
    },
    "keyword": {
        "keyword",
        "کلمه کلیدی",
        "کلیدواژه",
        "کلید واژه",
        "کليدواژه",
        "کلید",
    },
    "pillar": {
        "pillar",
        "related pillar",
        "pillar page",
        "ستون",
        "ستون مرتبط",
        "ستون اصلی",
        "پیلار",
    },
    "cluster": {
        "cluster",
        "خوشه",
        "خوشه محتوا",
        "خوشه موضوع",
        "کلاستر",
    },
    "type": {
        "type",
        "نوع",
        "نوع محتوا",
        "نوع مقاله",
    },
    "priority": {
        "priority",
        "اولویت",
        "الویت",
        "اولویت انتشار",
    },
    "week": {
        "week",
        "هفته",
        "هفته انتشار",
        "هفته برنامه",
    },
    "url": {
        "url",
        "link",
        "permalink",
        "لینک",
        "پیوند",
        "آدرس",
        "آدرس مقاله",
    },
}

# cells that look like headers but have no import target (ignored safely)
_IGNORABLE_HEADERS = {
    "id",
    "شناسه",
    "ردیف",
    "#",
    "written",
    "نوشته شده",
    "نوشتهشده",
    "published",
    "منتشر شده",
    "منتشرشده",
    "status",
    "وضعیت",
    "ای دی",
}

TYPE_LABELS = {
    "مقاله": "article",
    "صفحه ستون": "pillar_page",
    "پیلار پیج": "pillar_page",
    "راهنما": "guide",
    "صفحه راهنما": "guide",
    "خبر": "news",
    "اخبار": "news",
    "مقاله علمی": "article",
    "مقاله تحلیلی": "article",
    "مقاله آموزشی": "article",
}

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _normalize_alias(value: str) -> str:
    """Normalize a header cell for alias matching (lowercase, no spaces)."""
    return " ".join(str(value).strip().lower().replace("ي", "ی").replace("ك", "ک").split())


_ALIAS_LOOKUP: dict[str, str] = {}
for _field, _aliases in HEADER_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_normalize_alias(_alias)] = _field


def _to_int(value: Any) -> int | None:
    """Parse a CSV cell into an int (Persian digits, stray symbols tolerated)."""
    text = str(value or "").translate(_FA_DIGITS).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _count_outside_quotes(line: str, needle: str) -> int:
    count = 0
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == needle and not in_quotes:
            count += 1
    return count


def detect_delimiter(text: str) -> str:
    """Pick the delimiter used by the first non-empty line (quote aware)."""
    for line in text.splitlines():
        if not line.strip():
            continue
        line = line.lstrip("\ufeff")
        counts = [
            ("\t", _count_outside_quotes(line, "\t")),
            (",", _count_outside_quotes(line, ",")),
            (";", _count_outside_quotes(line, ";")),
            ("|", _count_outside_quotes(line, "|")),
        ]
        # Persian ، / ؛ separators (Excel-pasted Persian CSVs). Trusted when
        # the line has no ASCII spaces OR the comma appears ≥2 times — a
        # sentence with a single ، must stay one column, but a 5-column
        # Persian header row (هفته،عنوان،…) is unambiguous.
        fa_comma = _count_outside_quotes(line, "،")
        if " " not in line or fa_comma >= 2:
            counts.append(("،", fa_comma))
            counts.append(("؛", _count_outside_quotes(line, "؛")))
        # tab wins on any tab; otherwise the most frequent of the rest
        if counts[0][1] > 0:
            return "\t"
        best = max(counts[1:], key=lambda pair: pair[1])
        if best[1] > 0:
            return best[0]
        return ","
    return ","


def parse_delimited(text: str) -> tuple[str, list[list[str]]]:
    """Split raw pasted/file text into rows of cells. Returns (delimiter, rows)."""
    if not text:
        return ",", []
    text = text.lstrip("\ufeff")
    delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
    rows = [[cell.strip() for cell in row] for row in reader]
    rows = [row for row in rows if any(cell for cell in row)]
    return delimiter, rows


def detect_header(rows: list[list[str]]) -> bool:
    """True when the first row is a recognizable column header (≥2 known cells)."""
    if not rows:
        return False
    known = {_normalize_alias(cell) for cell in rows[0]}
    matched = sum(1 for cell in known if cell in _ALIAS_LOOKUP)
    return matched >= 2


def _column_name(rows: list[list[str]], index: int, has_header: bool) -> str:
    if has_header and rows:
        header = rows[0][index] if index < len(rows[0]) else ""
        if header:
            return header
    return f"ستون {index + 1}"


def detect_columns(rows: list[list[str]], has_header: bool) -> list[dict[str, Any]]:
    """Describe every detected column (name + a couple of sample values)."""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    columns: list[dict[str, Any]] = []
    for index in range(width):
        name = _column_name(rows, index, has_header)
        data_start = 1 if has_header else 0
        samples = [
            rows[row_idx][index]
            for row_idx in range(data_start, min(data_start + 2, len(rows)))
            if index < len(rows[row_idx]) and rows[row_idx][index]
        ]
        columns.append(
            {
                "index": index,
                "name": name,
                "samples": samples,
                "ignored": _normalize_alias(name) in _IGNORABLE_HEADERS,
            }
        )
    return columns


def build_column_map(columns: list[dict[str, Any]], has_header: bool) -> dict[str, int]:
    """Auto-map columns to fields by header alias; no-header → col 0 is title."""
    mapped: dict[str, int] = {}
    if has_header:
        for column in columns:
            field = _ALIAS_LOOKUP.get(_normalize_alias(column["name"]))
            if field and field not in mapped:
                mapped[field] = column["index"]
    elif columns:
        mapped["title"] = 0
    return mapped


# ---------------------------------------------------------------------------
# Row → topic payload
# ---------------------------------------------------------------------------
def _cell(row: list[str], column_map: dict[str, int], field: str) -> str:
    index = column_map.get(field)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def normalize_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def build_payload(
    row: list[str],
    column_map: dict[str, int],
    existing_titles: set[str],
    existing_keywords: set[str],
    batch_titles: set[str],
    batch_keywords: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Translate one CSV row into a topic payload.

    Returns ``(payload, None)`` on success, ``(payload, "coerced")`` when the
    row's type label was unrecognized (fallback type is used), ``(None,
    "duplicate")`` when the row matches an existing/batch title or keyword, or
    ``(None, message)`` on a validation error.
    """
    title = _cell(row, column_map, "title")
    if not title:
        return None, "عنوان خالی است"
    norm_title = normalize_key(title)
    if norm_title in existing_titles or norm_title in batch_titles:
        return None, "duplicate"

    keyword = _cell(row, column_map, "keyword")
    norm_keyword = normalize_key(keyword)
    if norm_keyword and (norm_keyword in existing_keywords or norm_keyword in batch_keywords):
        return None, "duplicate"

    topic_type = _cell(row, column_map, "type")
    coerced = False
    if topic_type:
        topic_type = TYPE_LABELS.get(topic_type, topic_type)
        if topic_type not in TOPIC_TYPES:
            # Editorial CSVs carry free-form type labels (راهنمای عیب‌یابی,
            # پرسش و پاسخ، …). Coerce to the default instead of failing the
            # row; the import summary reports how many rows were coerced.
            topic_type = "article"
            coerced = True

    payload: dict[str, Any] = {
        "title": title,
        "keyword": keyword,
        "pillar": _cell(row, column_map, "pillar"),
        "cluster": _cell(row, column_map, "cluster"),
        "type": topic_type or "article",
        "priority": _to_int(_cell(row, column_map, "priority")) or 0,
        "url": _cell(row, column_map, "url"),
        "week": _to_int(_cell(row, column_map, "week")),
    }

    batch_titles.add(norm_title)
    if norm_keyword:
        batch_keywords.add(norm_keyword)
    return payload, ("coerced" if coerced else None)


# ---------------------------------------------------------------------------
# Import runner
# ---------------------------------------------------------------------------
def import_rows(
    pb: Any,
    project_id: str,
    rows: list[list[str]],
    column_map: dict[str, int],
    has_header: bool,
    *,
    max_errors: int = 30,
) -> dict[str, Any]:
    """Create topics from parsed rows. Returns a summary for the result UI.

    ``rows`` includes the header row when ``has_header`` is true (it is
    skipped). Duplicates (existing in the project or earlier in this batch)
    are skipped silently; per-row validation/DB errors are collected.
    """
    repo = TopicRepo(pb)
    existing_titles, existing_keywords = repo.existing_keys(project_id)
    batch_titles: set[str] = set()
    batch_keywords: set[str] = set()

    created = 0
    skipped = 0
    coerced = 0
    errors: list[dict[str, Any]] = []
    data_rows = rows[1:] if has_header else rows

    for offset, row in enumerate(data_rows, start=1):
        line_no = offset + (1 if has_header else 0)
        payload, reason = build_payload(
            row,
            column_map,
            existing_titles,
            existing_keywords,
            batch_titles,
            batch_keywords,
        )
        if reason == "duplicate":
            skipped += 1
            continue
        if reason == "coerced":
            coerced += 1
        if reason not in (None, "coerced"):
            if len(errors) < max_errors:
                errors.append({"row": line_no, "reason": reason})
            continue
        assert payload is not None
        try:
            repo.create(project=project_id, **payload)
            created += 1
        except Exception as exc:  # noqa: BLE001 - keep importing the rest
            if len(errors) < max_errors:
                errors.append({"row": line_no, "reason": f"ذخیره ناموفق: {exc}"})

    return {
        "total": len(data_rows),
        "created": created,
        "skipped": skipped,
        "coerced": coerced,
        "errors": errors,
        "error_count": len(errors),
        "has_more_errors": len(errors) >= max_errors,
    }


# ---------------------------------------------------------------------------
# Preview hand-off cache (in-process, one-time tokens)
# ---------------------------------------------------------------------------
_PREVIEW_CACHE: dict[str, dict[str, Any]] = {}
_PREVIEW_TTL = 10 * 60  # seconds
_PREVIEW_MAX = 60


def _evict_cache(now: float) -> None:
    stale = [k for k, v in _PREVIEW_CACHE.items() if now - v["created"] > _PREVIEW_TTL]
    for key in stale:
        _PREVIEW_CACHE.pop(key, None)
    while len(_PREVIEW_CACHE) > _PREVIEW_MAX:
        oldest = min(_PREVIEW_CACHE, key=lambda k: _PREVIEW_CACHE[k]["created"])
        _PREVIEW_CACHE.pop(oldest, None)


def store_preview(
    *,
    rows: list[list[str]],
    delimiter: str,
    has_header: bool,
    columns: list[dict[str, Any]],
    filename: str = "",
) -> str:
    now = time.monotonic()
    _evict_cache(now)
    token = uuid.uuid4().hex
    _PREVIEW_CACHE[token] = {
        "rows": rows,
        "delimiter": delimiter,
        "has_header": has_header,
        "columns": columns,
        "filename": filename,
        "created": now,
    }
    return token


def get_preview(token: str) -> dict[str, Any] | None:
    entry = _PREVIEW_CACHE.get(token or "")
    if not entry:
        return None
    if time.monotonic() - entry["created"] > _PREVIEW_TTL:
        _PREVIEW_CACHE.pop(token, None)
        return None
    return entry
