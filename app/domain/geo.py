"""Country + locale option lists for the Localization & brand settings.

Backed by Babel's CLDR data (already a project dependency) so the selects
carry every ISO 3166-1 country and every CLDR locale — no hand-maintained
list that drifts. A stored value not in the canonical list is prepended so a
pre-existing free-text setting is never silently dropped.
"""

from __future__ import annotations

from functools import lru_cache

import babel.localedata
from babel import Locale


@lru_cache(maxsize=1)
def _countries() -> list[tuple[str, str]]:
    territories = Locale.parse("en").territories
    return sorted(
        (
            (code, name)
            for code, name in territories.items()
            if len(code) == 2 and code.isalpha() and code.isupper()
        ),
        key=lambda item: item[1],
    )


def country_options(stored: str = "") -> list[tuple[str, str]]:
    """[(code, country name)] sorted by name, with a legacy stored value first."""
    options = list(_countries())
    value = (stored or "").strip()
    if value and value not in {code for code, _ in options}:
        options.insert(0, (value, value))
    return options


@lru_cache(maxsize=1)
def _locales() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for identifier in babel.localedata.locale_identifiers():
        tag = identifier.replace("_", "-")
        try:
            label = Locale.parse(identifier).get_display_name("en") or tag
        except Exception:
            label = tag
        out.append((tag, f"{tag} — {label}"))
    return sorted(set(out), key=lambda item: item[0])


def locale_options(stored: str = "") -> list[tuple[str, str]]:
    """[(BCP-47 tag, 'tag — display name')], with a legacy stored value first."""
    options = list(_locales())
    value = (stored or "").strip()
    if value and value not in {tag for tag, _ in options}:
        options.insert(0, (value, value))
    return options
