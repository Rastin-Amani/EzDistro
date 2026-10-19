"""Template contract: a self-replacing HTMX container must not wrap links.

``hx-target``, ``hx-swap`` and ``hx-select`` are INHERITED by descendant htmx
requests. So a container like::

    <div id="article-body" hx-get="…" hx-target="this" hx-swap="outerHTML"
         hx-select="#article-body"> … <a href="/projects/1?tab=articles"> … </div>

makes the back button ask for another page and then select ``#article-body``
from a response that has no such element — an empty swap and a blank screen.
The refresh trigger must be a hidden SIBLING of the container instead (the
pattern used in pages/research/run.html).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _elements_with_attr(text: str, needle: str):
    """Yield (tag, open_start, open_end, close_start) for elements whose opening
    tag contains `needle`, using a balanced scan for the same tag name."""
    for m in re.finditer(r"<(\w+)([^>]*" + re.escape(needle) + r"[^>]*)>", text, re.S):
        tag = m.group(1)
        depth, i = 0, m.start()
        pattern = re.compile(rf"<{tag}\b|</{tag}>")
        close_start = None
        while True:
            step = pattern.search(text, i)
            if step is None:
                break
            depth += 1 if step.group(0).startswith(f"<{tag}") else -1
            i = step.end()
            if depth == 0:
                close_start = step.start()
                break
        if close_start is None:
            continue
        yield tag, m.end(), close_start


@pytest.mark.parametrize("path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: str(p.name))
def test_self_replacing_container_does_not_wrap_links(path: Path):
    text = path.read_text()
    offenders: list[str] = []

    for needle in ('hx-target="this"', 'hx-select="'):
        for _tag, body_start, body_end in _elements_with_attr(text, needle):
            body = text[body_start:body_end]
            link = re.search(r'<a\s[^>]*href="', body, re.S)
            if link:
                line = text[:body_start].count("\n") + 1
                snippet = " ".join(body[link.start() : link.start() + 60].split())
                offenders.append(f"line {line}: contains {snippet}")

    assert not offenders, (
        f"{path.relative_to(TEMPLATES)} has an inherited-target container wrapping a link "
        f"(blank page on click). Move the refresh trigger to a hidden sibling.\n  "
        + "\n  ".join(offenders)
    )
