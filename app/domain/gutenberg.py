"""Convert article HTML into WordPress Gutenberg block markup.

The article pipeline stores plain semantic HTML (sanitized, human-readable,
theme-agnostic). WordPress themes style *blocks* — a raw ``<table>`` or
``<blockquote>`` can render unstyled. This module converts the stored HTML to
Gutenberg block comments at publish time so tables, quotes, images, lists and
the trailing "Related" list obey the site's theme.

Pure and dependency-free (stdlib ``html.parser``). Only applied on the way
out to WordPress — the stored article and the admin preview stay plain HTML.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_RELATED_RE = re.compile(
    r"^(related(\s+(articles?|posts?|reading))?|further reading|read (next|more)|"
    r"you might also like|see also)\b",
    re.IGNORECASE,
)


class _Splitter(HTMLParser):
    """Split a fragment into top-level blocks, preserving inline markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self._depth = 0
        self._tag = ""
        self._buf: list[str] = []

    def _push(self, raw: str) -> None:
        if self._depth == 0:
            self._tag = ""
            self._buf = [raw]
        else:
            self._buf.append(raw)

    def handle_starttag(self, tag: str, attrs: object) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        if tag in _VOID:
            if self._depth == 0:
                self.blocks.append({"tag": tag, "html": raw, "text": ""})
            else:
                self._buf.append(raw)
            return
        if self._depth == 0:
            self._tag = tag
            self._buf = [raw]
        else:
            self._buf.append(raw)
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        raw = self.get_starttag_text() or f"<{tag}/>"
        if self._depth == 0:
            self.blocks.append({"tag": tag, "html": raw, "text": ""})
        else:
            self._buf.append(raw)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        self._buf.append(f"</{tag}>")
        self._depth -= 1
        if self._depth == 0:
            self.blocks.append({"tag": self._tag, "html": "".join(self._buf), "text": ""})
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._depth == 0:
            if data.strip():
                self.blocks.append(
                    {"tag": "", "html": _html.escape(data, quote=False), "text": data}
                )
        else:
            self._buf.append(_html.escape(data, quote=False))

    def finish(self) -> None:
        # Tolerate unbalanced input: emit whatever is still open as raw HTML.
        if self._depth > 0 and self._buf:
            self.blocks.append({"tag": "", "html": "".join(self._buf), "text": ""})


def _add_class(raw: str, tag: str, css_class: str) -> str:
    """Add (or extend) a class on the first opening <tag> in ``raw``."""
    match = re.search(rf"<{tag}\b[^>]*>", raw, re.IGNORECASE)
    if not match:
        return raw
    opening = match.group(0)
    if re.search(r"class\s*=", opening, re.IGNORECASE):
        new_opening = re.sub(
            r'class\s*=\s*"([^"]*)"',
            lambda m: f'class="{m.group(1)} {css_class}"',
            opening,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        closing = "/>" if opening.rstrip().endswith("/>") else ">"
        new_opening = opening.rstrip()[: -len(closing)].rstrip() + f' class="{css_class}"{closing}'
    return raw[: match.start()] + new_opening + raw[match.end() :]


def _strip_tags(raw: str) -> str:
    return re.sub(r"<[^>]+>", "", raw).strip()


def _wrap(tag: str, raw: str) -> str:
    if tag in _HEADINGS:
        level = int(tag[1])
        return f'<!-- wp:heading {{"level":{level}}} -->\n{raw}\n<!-- /wp:heading -->'
    if tag == "p":
        return f"<!-- wp:paragraph -->\n{raw}\n<!-- /wp:paragraph -->"
    if tag in ("ul", "ol"):
        return f"<!-- wp:list -->\n{raw}\n<!-- /wp:list -->"
    if tag == "blockquote":
        inner = _add_class(raw, "blockquote", "wp-block-quote")
        return f"<!-- wp:quote -->\n{inner}\n<!-- /wp:quote -->"
    if tag == "table":
        return (
            f'<!-- wp:table -->\n<figure class="wp-block-table">{raw}</figure>\n<!-- /wp:table -->'
        )
    if tag == "pre":
        inner = _add_class(raw, "pre", "wp-block-code")
        return f"<!-- wp:code -->\n{inner}\n<!-- /wp:code -->"
    if tag == "hr":
        return (
            "<!-- wp:separator -->\n"
            '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
            "<!-- /wp:separator -->"
        )
    if tag == "figure":
        inner = _add_class(raw, "figure", "wp-block-image")
        inner = re.sub(
            r"<figcaption\b(?![^>]*class=)",
            '<figcaption class="wp-element-caption"',
            inner,
            count=1,
            flags=re.IGNORECASE,
        )
        return f"<!-- wp:image -->\n{inner}\n<!-- /wp:image -->"
    if tag == "img":
        return (
            f'<!-- wp:image -->\n<figure class="wp-block-image">{raw}</figure>\n<!-- /wp:image -->'
        )
    return f"<!-- wp:html -->\n{raw}\n<!-- /wp:html -->"


def _paragraph(text: str) -> str:
    return f"<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->"


def to_gutenberg_blocks(html: str) -> str:
    """Return Gutenberg block markup for a plain-HTML article body."""
    if not html or not html.strip():
        return ""
    splitter = _Splitter()
    splitter.feed(html)
    splitter.close()
    splitter.finish()

    items = [
        {
            "tag": block["tag"],
            "raw": block["html"],
            "text": _strip_tags(block["html"]) if block["tag"] in _HEADINGS else block["text"],
            "wrapped": _paragraph(block["html"])
            if not block["tag"]
            else _wrap(block["tag"], block["html"]),
        }
        for block in splitter.blocks
    ]

    out: list[str] = []
    i = 0
    while i < len(items):
        item = items[i]
        if item["tag"] in _HEADINGS and _RELATED_RE.match(item["text"] or ""):
            group = [item["wrapped"]]
            j = i + 1
            if j < len(items) and items[j]["tag"] in ("ul", "ol"):
                group.append(items[j]["wrapped"])
                j += 1
            out.append(
                '<!-- wp:group {"className":"ezdistro-related","layout":{"type":"constrained"}} -->\n'
                '<div class="wp-block-group ezdistro-related">\n' + "\n".join(group) + "\n</div>\n"
                "<!-- /wp:group -->"
            )
            i = j
            continue
        out.append(item["wrapped"])
        i += 1
    return "\n\n".join(out)
