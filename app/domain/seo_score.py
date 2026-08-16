"""Deterministic SEO score heuristic (0–100).

No LLM calls: keyword presence in title/H1/first paragraph/headings, heading
structure, word count, meta description, internal links.
"""

from __future__ import annotations

import re

from app.domain.chunker import strip_html


def seo_score(*, title: str, meta_description: str, html: str, keyword: str) -> int:
    if not keyword:
        return _structure_score(html, title, meta_description)

    score = 0
    kw = keyword.strip().lower()
    text = strip_html(html).lower()

    # 1. keyword in title (25)
    if kw in title.lower():
        score += 25

    # 2. keyword in first paragraph (15)
    first_para = _first_paragraph(html).lower()
    if kw in first_para:
        score += 15

    # 3. keyword in headings (20)
    headings = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html, re.IGNORECASE | re.DOTALL)
    if any(kw in re.sub(r"<[^>]+>", "", h).lower() for h in headings):
        score += 20

    # 4. keyword density sane (10): 0.5%–3% of words
    words = len(text.split())
    occurrences = text.count(kw)
    if words > 0:
        density = occurrences / words
        if 0.005 <= density <= 0.03:
            score += 10

    score += _structure_score(html, title, meta_description)
    return max(0, min(100, score))


def _structure_score(html: str, title: str, meta_description: str) -> int:
    score = 0
    if title.strip():
        score += 10
    if meta_description.strip():
        score += 10
    headings = re.findall(r"<h2[^>]*>", html, re.IGNORECASE)
    if 2 <= len(headings) <= 10:
        score += 10
    words = len(strip_html(html).split())
    if words >= 500:
        score += 10
    if re.search(r"<ul|<ol", html, re.IGNORECASE):
        score += 5
    if re.search(r'<a href="https?://', html, re.IGNORECASE):
        score += 5
    return score


def _first_paragraph(html: str) -> str:
    match = re.search(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"<[^>]+>", "", match.group(1)) if match else ""
