"""Locale registry + translation plumbing.

This module is the single authoritative i18n architecture:

- ``LOCALES``      — one registry of BCP 47 locale metadata (direction, names,
                     enabled state). Adding a language = add an entry + a
                     catalog under ``app/locales/<code>/LC_MESSAGES/``.
- ``_()``          — gettext translation. Persian (``fa``) is the *source*
                     language: msgids in code/templates are Persian, and any
                     locale without a compiled catalog falls back to the
                     source strings (fails safe, never crashes).
- ``get_locale()`` — request-scoped locale, set by middleware via contextvar.

No URL prefixes by design: this is an authenticated dashboard (no public/SEO
pages), so the explicit user choice travels in the ``locale`` cookie and every
relative link/HTMX URL keeps working unchanged.
"""

from __future__ import annotations

import gettext as _stdlib_gettext
import os
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Locale:
    code: str  # canonical BCP 47 tag, key into LOCALES
    native_name: str
    direction: str  # "rtl" | "ltr"
    enabled: bool = False
    default: bool = False
    flag: str = ""  # emoji shown in the language switcher

    @property
    def is_rtl(self) -> bool:
        return self.direction == "rtl"


# One authoritative registry. Disabled entries are readiness declarations:
# enabling a language = flip `enabled` + ship app/locales/<code>/LC_MESSAGES/messages.mo
LOCALES: dict[str, Locale] = {
    "en": Locale("en", "English", "ltr", enabled=True, default=True, flag="🇬🇧"),
    "fa": Locale("fa", "Persian", "rtl", enabled=True, flag="🇮🇷"),
    # --- prepared but disabled (same workflow, no architecture changes) ---
    "hy": Locale("hy", "Հայերեն", "ltr", flag="🇦🇲"),
    "ar": Locale("ar", "Arabic", "rtl", flag="🇸🇦"),
    "es": Locale("es", "Español", "ltr", flag="🇪🇸"),
    "ru": Locale("ru", "Русский", "ltr", flag="🇷🇺"),
    "de": Locale("de", "Deutsch", "ltr", flag="🇩🇪"),
    "fr": Locale("fr", "Français", "ltr", flag="🇫🇷"),
    "tr": Locale("tr", "Türkçe", "ltr", flag="🇹🇷"),
    "pt-BR": Locale("pt-BR", "Português (Brasil)", "ltr", flag="🇧🇷"),
    "zh-CN": Locale("zh-CN", "简体中文", "ltr", flag="🇨🇳"),
    "ja": Locale("ja", "日本語", "ltr", flag="🇯🇵"),
    "ko": Locale("ko", "한국어", "ltr", flag="🇰🇷"),
    "hi": Locale("hi", "हिन्दी", "ltr", flag="🇮🇳"),
}

DEFAULT_LOCALE = next(loc for loc in LOCALES.values() if loc.default)
ENABLED_LOCALES: dict[str, Locale] = {c: loc for c, loc in LOCALES.items() if loc.enabled}
LOCALE_COOKIE = "locale"

_LOCALEDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

_current: ContextVar[str] = ContextVar("current_locale", default=DEFAULT_LOCALE.code)

# Compiled catalogs, loaded once (immutable read-mostly app data — safe to cache).
_translations: dict[str, _stdlib_gettext.NullTranslations] = {}


def _get_translation(code: str) -> _stdlib_gettext.NullTranslations:
    translation = _translations.get(code)
    if translation is None:
        try:
            translation = _stdlib_gettext.translation("messages", _LOCALEDIR, [code])
        except FileNotFoundError:
            # Missing/uncompiled catalog fails safe: source (Persian) strings.
            translation = _stdlib_gettext.NullTranslations()
        _translations[code] = translation
    return translation


def get_locale() -> Locale:
    """Current request locale (allowlisted; unknown values collapse to default)."""
    return LOCALES.get(_current.get(), DEFAULT_LOCALE)


def set_request_locale(code: str | None) -> None:
    """Validate against the allowlist and bind the request locale. Middleware-only."""
    if code and code in ENABLED_LOCALES:
        _current.set(code)
    else:
        _current.set(DEFAULT_LOCALE.code)


def _(message: str) -> str:
    """Translate a UI string (msgids are Persian source strings)."""
    return _get_translation(_current.get()).gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a count-aware message; interpolate with %(n)s at the call site."""
    return _get_translation(_current.get()).ngettext(singular, plural, n)


class _LocaleProxy:
    """Template-facing view of the current locale, resolved at render time."""

    @property
    def code(self) -> str:
        return get_locale().code

    @property
    def direction(self) -> str:
        return get_locale().direction

    @property
    def is_rtl(self) -> bool:
        return get_locale().is_rtl

    @property
    def native_name(self) -> str:
        return get_locale().native_name

    def enabled(self) -> list[Locale]:
        return [loc for loc in LOCALES.values() if loc.enabled]


locale_proxy = _LocaleProxy()
