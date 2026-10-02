"""Keyword Planner file import: CSV / XLSX -> `KeywordIdea` rows.

Why this exists: many users cannot get Google Ads API access (developer token /
Cloud project approval). They can still export a Keyword Planner CSV or XLSX and
upload it here. The imported metrics are *source data* — they are never invented
or estimated. AI analysis (intent, clustering, opportunities) happens later in
the same pipeline the API path feeds.

Pure I/O-free-ish parsing: stdlib only (`csv`, `zipfile`, `xml.etree`), so XLSX
support costs zero new dependencies.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

from app.domain.keywords import normalize_keyword
from app.providers.base import KeywordIdea

# Column aliases (normalized: lowercase, non-alphanumerics collapsed to spaces).
# Deliberately generous — Keyword Planner exports vary by language and version.
_KEYWORD_ALIASES = (
    "keyword",
    "keywords",
    "search terms",
    "search term",
    "query",
    "term",
    "keword",
    "keyword text",
)
_VOLUME_ALIASES = (
    "avg monthly searches",
    "average monthly searches",
    "avg monthly search volume",
    "monthly searches",
    "search volume",
    "volume",
    "avg searches",
    "average searches",
    "searches",
    "impressions",
)
_COMPETITION_ALIASES = (
    "competition",
    "competition level",
    "comp",
    "ad competition",
)
_INDEX_ALIASES = (
    "competition indexed value",
    "competition index",
    "indexed value",
    "index",
)
_CPC_ALIASES = (
    "avg cpc",
    "average cpc",
    "cpc",
    "top of page bid low range",
    "top of page bid high range",
    "low top of page bid",
    "high top of page bid",
    "suggested bid",
    "bid",
    "cost per click",
)
_CURRENCY_ALIASES = ("currency", "currency code")
_MONTH_ALIASES = ("month", "monthly search volume", "period", "date")

_FIELDS = ("keyword", "volume", "competition", "index", "cpc_low", "cpc_high", "currency")

# Keyword Planner writes numbers like "12,000", "1.2K", or "1,234.56".
_NUMBER_RE = re.compile(r"-?\d[\d,.\s]*")
_MICRO = 1_000_000


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _alias_lookup(header: str, aliases: tuple[str, ...]) -> bool:
    head = _norm(header)
    if not head:
        return False
    if head in aliases:
        return True
    # Allow "avg. monthly searches (US)" style suffixes/prefixes.
    return any(head.startswith(alias) or head.endswith(alias) for alias in aliases)


def _map_headers(headers: list[str]) -> dict[str, int]:
    """Return field -> column index for the ones we can recognise."""
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        for field in _FIELDS:
            if field in mapping:
                continue
            aliases: tuple[str, ...]
            if field == "keyword":
                aliases = _KEYWORD_ALIASES
            elif field == "volume":
                aliases = _VOLUME_ALIASES
            elif field == "competition":
                aliases = _COMPETITION_ALIASES
            elif field == "index":
                aliases = _INDEX_ALIASES
            elif field == "currency":
                aliases = _CURRENCY_ALIASES
            else:
                aliases = _CPC_ALIASES
            if _alias_lookup(header, aliases):
                mapping[field] = index
                break
    return mapping


def _to_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    match = _NUMBER_RE.search(text)
    if not match:
        return 0.0
    cleaned = match.group(0).replace(",", "").replace(" ", "").strip(".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _to_micros(value: Any, currency: str) -> int:
    """CPC/bid -> micros. Treats bare numbers as major units (2.5 -> 2_500_000)."""
    number = _to_number(value)
    if number <= 0:
        return 0
    return int(round(number * _MICRO))


_COMPETITION_MAP = {
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "": "UNSPECIFIED",
    "unknown": "UNSPECIFIED",
    "unspecified": "UNSPECIFIED",
}


def _clean_competition(value: Any) -> str:
    text = _norm(value)
    if text in _COMPETITION_MAP:
        return _COMPETITION_MAP[text]
    digits = _to_number(value)
    if digits > 0:
        # Numeric competition index masquerading as the level column.
        if digits >= 67:
            return "HIGH"
        if digits >= 34:
            return "MEDIUM"
        return "LOW"
    return "UNSPECIFIED"


# ---------------------------------------------------------------------------
# XLSX (stdlib only)
# ---------------------------------------------------------------------------
def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    strings: list[str] = []
    root = ElementTree.fromstring(raw)
    for si in root:
        # Concatenate every <t> under the <si> (rich text runs split words).
        parts = [node.text or "" for node in si.iter() if node.tag.endswith("}t")]
        strings.append("".join(parts))
    return strings


def _xlsx_first_sheet(archive: zipfile.ZipFile) -> bytes:
    names = archive.namelist()
    candidates = ["xl/worksheets/sheet1.xml"]
    candidates += sorted(
        name for name in names if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
    )
    for name in candidates:
        if name in names:
            return archive.read(name)
    raise ValueError("the XLSX file has no worksheet")


def _xlsx_cell_value(cell: ElementTree.Element, shared: list[str]) -> Any:
    ctype = cell.get("t")
    if ctype == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}v"))
    value_node = next((node for node in cell if node.tag.endswith("}v")), None)
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if ctype == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _column_index(ref: str) -> int:
    """A1-style reference -> zero-based column index."""
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(index - 1, 0)


def _read_xlsx(data: bytes) -> list[list[Any]]:
    """Minimal XLSX reader: returns a grid of strings/numbers (first sheet)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared = _xlsx_shared_strings(archive)
        sheet = _xlsx_first_sheet(archive)

    root = ElementTree.fromstring(sheet)
    rows: list[list[Any]] = []
    for row in root.iter():
        if not row.tag.endswith("}row"):
            continue
        cells: dict[int, Any] = {}
        for cell in row:
            if not cell.tag.endswith("}c"):
                continue
            index = _column_index(cell.get("r") or "")
            cells[index] = _xlsx_cell_value(cell, shared)
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _read_csv(data: bytes) -> list[list[Any]]:
    text = _decode(data)
    # Keyword Planner appends preamble rows before the real header; sniff the
    # dialect, then let header detection skip anything that isn't a header.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return [list(row) for row in reader]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _find_header(grid: list[list[Any]]) -> tuple[int, dict[str, int]]:
    """Locate the header row: the first row that maps a keyword column."""
    best_row = -1
    best_map: dict[str, int] = {}
    for index, row in enumerate(grid[:20]):
        mapping = _map_headers([str(cell) for cell in row])
        if "keyword" in mapping and len(mapping) > len(best_map):
            best_row, best_map = index, mapping
    return best_row, best_map


def _rows_to_ideas(
    grid: list[list[Any]], header_index: int, mapping: dict[str, int], currency: str
) -> tuple[list[KeywordIdea], int]:
    ideas: dict[str, KeywordIdea] = {}
    skipped = 0
    for row in grid[header_index + 1 :]:
        if not any(str(cell).strip() for cell in row):
            continue
        keyword_index = mapping["keyword"]
        text = str(row[keyword_index]).strip() if keyword_index < len(row) else ""
        if not text:
            skipped += 1
            continue
        key = normalize_keyword(text)
        if not key:
            skipped += 1
            continue

        def cell(field: str, row: list[Any] = row) -> Any:
            index = mapping.get(field)
            if index is None or index >= len(row):
                return ""
            return row[index]

        idea = KeywordIdea(
            text=text,
            avg_monthly_searches=int(_to_number(cell("volume"))),
            competition=_clean_competition(cell("competition")),
            competition_index=int(_to_number(cell("index"))),
            average_cpc_micros=_to_micros(cell("cpc_low"), currency),
            low_top_of_page_bid_micros=_to_micros(cell("cpc_low"), currency),
            high_top_of_page_bid_micros=_to_micros(cell("cpc_high") or cell("cpc_low"), currency),
            currency_code=str(cell("currency") or currency or "").strip(),
            source="keyword_planner_import",
        )
        current = ideas.get(key)
        # Keep the row with the larger volume; imported facts are never summed.
        if current is None or idea.avg_monthly_searches > current.avg_monthly_searches:
            ideas[key] = idea
    return list(ideas.values()), skipped


def parse_keyword_file(
    data: bytes,
    filename: str = "",
    *,
    mapping: dict[str, int] | None = None,
) -> tuple[list[KeywordIdea], dict[str, Any]]:
    """Parse an uploaded Keyword Planner export into `KeywordIdea` rows.

    Returns `(ideas, report)`. `report` describes what was detected so the UI can
    show columns + counts, and can be re-used to drive a manual column mapping
    when `report["ok"]` is false.
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        grid = _read_xlsx(data)
    elif name.endswith(".xls"):
        raise ValueError("legacy .xls is not supported — export as .xlsx or .csv")
    else:
        grid = _read_csv(data)

    if not grid:
        raise ValueError("the uploaded file is empty")

    header_index, detected = _find_header(grid)
    if mapping:
        # Manual mapping wins (values are header *names* or indices).
        headers = [str(cell) for cell in grid[header_index]] if header_index >= 0 else []
        resolved: dict[str, int] = {}
        for field, target in mapping.items():
            if isinstance(target, int):
                resolved[field] = target
            elif str(target).strip():
                wanted = _norm(target)
                for index, header in enumerate(headers):
                    if _norm(header) == wanted:
                        resolved[field] = index
                        break
        detected = resolved

    report: dict[str, Any] = {
        "filename": filename,
        "format": "xlsx" if name.endswith(("xlsx", "xlsm")) else "csv",
        "headerRow": header_index,
        "columns": detected,
        "ok": "keyword" in detected,
    }
    if header_index < 0 or "keyword" not in detected:
        report["ideas"] = 0
        report["skipped"] = 0
        return [], report

    headers = [str(cell).strip() for cell in grid[header_index]]
    report["headers"] = [header for header in headers if header]
    currency = _guess_currency(grid[header_index + 1 :] if header_index + 1 < len(grid) else [])
    ideas, skipped = _rows_to_ideas(grid, header_index, detected, currency)
    report["ideas"] = len(ideas)
    report["skipped"] = skipped
    return ideas, report


def _guess_currency(rows: list[list[Any]]) -> str:
    for row in rows[:50]:
        for cell in row:
            text = str(cell or "").strip()
            if len(text) == 3 and text.isalpha() and text.isupper():
                return text
    return ""


def _demo() -> None:  # pragma: no cover - runnable self check
    csv_bytes = (
        b"Keyword,Avg. monthly searches,Competition,Competition (indexed value),"
        b"Top of page bid (low range),Top of page bid (high range),Currency\n"
        b'gym management software,"8,100",Medium,61,1.50,4.20,USD\n'
        b"gym crm,1200,Low,12,0.80,2.00,USD\n"
        b'Gym Management Software,"8,100",Medium,61,1.50,4.20,USD\n'
    )
    ideas, report = parse_keyword_file(csv_bytes, "kw.csv")
    assert report["ok"] and report["format"] == "csv", report
    assert report["ideas"] == 2, report  # deduped case-insensitively
    assert report["skipped"] == 0, report
    by = {normalize_keyword(i.text): i for i in ideas}
    assert by["gym management software"].avg_monthly_searches == 8100
    assert by["gym management software"].average_cpc_micros == 1_500_000
    assert by["gym management software"].competition_index == 61
    assert by["gym management software"].competition == "MEDIUM"
    assert by["gym crm"].high_top_of_page_bid_micros == 2_000_000

    unknown, bad = parse_keyword_file(b"Nope,Nope\n1,2\n", "bad.csv")
    assert unknown == [] and bad["ok"] is False, bad

    xlsx = _build_demo_xlsx()
    grid = _read_xlsx(xlsx)
    assert grid[0][0] == "Keyword", grid
    assert grid[1][0] == "gym management software", grid
    xideas, xreport = parse_keyword_file(xlsx, "kw.xlsx")
    assert xreport["format"] == "xlsx" and xreport["ok"], xreport
    assert len(xideas) == 2 and xideas[0].avg_monthly_searches == 8100, xreport
    print("research_import ok")


def _build_demo_xlsx() -> bytes:  # pragma: no cover - fixture only
    shared = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" count="8" uniqueCount="8">'
        "<si><t>Keyword</t></si>"
        "<si><t>Avg. monthly searches</t></si>"
        "<si><t>Competition</t></si>"
        "<si><t>Currency</t></si>"
        "<si><t>gym management software</t></si>"
        "<si><t>gym crm</t></si>"
        "<si><t>Medium</t></si>"
        "<si><t>Low</t></si>"
        "</sst>"
    )
    sheet = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2"><v>8100</v></c>'
        '<c r="C2" t="s"><v>6</v></c><c r="D2" t="s"><v>3</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>5</v></c><c r="B3"><v>1200</v></c>'
        '<c r="C3" t="s"><v>7</v></c><c r="D3" t="s"><v>3</v></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


if __name__ == "__main__":  # pragma: no cover
    _demo()
