"""Content extraction & chunking — pure logic, no I/O.

Mirrors the original n8n Indexer behaviour and extends it into a configurable
pipeline:

- strip HTML → clean plain text (Persian-aware: ZWNJ handling)
- split into semantic units first (paragraphs → sentences), THEN pack units
  into word-budgeted chunks with overlap
- never blindly split Persian text at arbitrary character boundaries:
  paragraph-aware → sentence-aware → word-level hard fallback
- `max_chunk_count` caps the output (0 = unlimited)
"""

from __future__ import annotations

import html as html_mod
import re

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|iframe)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
_TRAILING_HYPHEN_RE = re.compile(r"\s*[-\u2014\u2013]\s*$")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
# Persian-aware sentence boundaries: . ! ? ؟ ! ؛ ; whitespace after a
# sentence end, or a line break itself.
_SENTENCE_RE = re.compile(r"(?<=[.!?؟!؛])\s+|\r?\n+")
# sentence-ending punctuation (used to decide hard-split points)
_SENTENCE_END = re.compile(r"[.!?؟!؛]$")

SEPARATOR_STRATEGIES = ("auto", "paragraph", "sentence", "word")


def strip_html(raw: str) -> str:
    """Remove scripts/styles/tags and return clean plain text."""
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\u200c", " ")  # ZWNJ → space so Persian words split correctly
    text = _WS_RE.sub(" ", text)
    text = _TRAILING_HYPHEN_RE.sub("", text)
    return text.strip()


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def count_words(text: str) -> int:
    return len(_words(text))


def word_count_from_html(raw_html: str) -> int:
    return count_words(strip_html(raw_html))


# ---------------------------------------------------------------------------
# Unit extraction (semantic boundaries first)
# ---------------------------------------------------------------------------
def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines; keeps whitespace-normalized paragraphs."""
    parts = _PARAGRAPH_RE.split(text)
    return [normalize_whitespace(p) for p in parts if normalize_whitespace(p)]


def split_sentences(text: str) -> list[str]:
    """Persian-aware sentence split: . ! ? ؟ ! ؛ and newlines as boundaries."""
    parts = _SENTENCE_RE.split(text)
    sentences: list[str] = []
    for part in parts:
        part = normalize_whitespace(part)
        if part:
            sentences.append(part)
    return sentences


def split_units(text: str, strategy: str) -> list[str]:
    """Extract semantic units per strategy.

    - paragraph: blank-line separated blocks
    - sentence: sentence boundaries (newlines also split)
    - word: individual words (last resort)
    - auto: paragraphs, falling back to sentences for oversized paragraphs
      (the packer re-splits oversized units when needed)
    """
    if strategy == "word":
        return _words(text)
    if strategy == "sentence":
        return split_sentences(text)
    # paragraph & auto both start from paragraphs; `auto` re-splits oversized
    # paragraphs into sentences during packing.
    return split_paragraphs(text)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_text(
    text: str,
    *,
    size: int = 500,
    overlap: int = 100,
    strategy: str = "auto",
    max_chunks: int = 0,
) -> list[str]:
    """Chunk text into word-budgeted chunks.

    Units (paragraphs/sentences/words) are packed greedily into chunks of at
    most `size` words. A unit that alone exceeds `size` is re-split:
    - auto: paragraph → sentences → hard word split
    - paragraph: hard word split (unavoidable for oversized paragraphs)
    - sentence: hard word split
    - word: plain word packing

    `overlap` carries the trailing units of the previous chunk into the next
    (word-budgeted, unit-aligned). `max_chunks` truncates the output.
    """
    if not text or size <= 0:
        return []
    size = max(1, int(size))
    overlap = max(0, min(int(overlap), size - 1))
    max_chunks = max(0, int(max_chunks))

    strategy = strategy if strategy in SEPARATOR_STRATEGIES else "auto"
    units = split_units(text, strategy)
    if not units:
        return []

    chunks: list[str] = []
    index = 0
    while index < len(units):
        chunk_units, index = _pack_units(units, index, size, overlap, strategy)
        chunk = " ".join(chunk_units)
        if chunk.strip():
            chunks.append(chunk)
        if max_chunks and len(chunks) >= max_chunks:
            break
        if index >= len(units):
            break
    return chunks


def _pack_units(
    units: list[str], start: int, size: int, overlap: int, strategy: str
) -> tuple[list[str], int]:
    """Greedily pack units into one chunk starting at `start`.

    Returns (chunk_units, next_start). Handles oversized units via
    `_split_oversized`. Overlap is unit-aligned: the next chunk re-includes
    the trailing units that cover `overlap` words of the current chunk.
    """
    chunk_units: list[str] = []
    words = 0
    index = start

    while index < len(units):
        unit = units[index]
        unit_words = count_words(unit)

        if unit_words > size:
            # Unit alone exceeds the budget → split it and continue.
            piece, remainder = _split_oversized(unit, unit_words, size, strategy)
            if chunk_units:
                # close the current chunk first; retry the piece next round
                break
            chunk_units.append(piece)
            if remainder is not None:
                units[index] = remainder  # type: ignore[assignment]  # str | None narrowed by check
                return chunk_units, index
            index += 1
            continue

        if words + unit_words > size and chunk_units:
            break
        chunk_units.append(unit)
        words += unit_words
        index += 1

    if not chunk_units:
        # degenerate: everything was oversized/empty
        return [" ".join(units[start : start + 1])], min(start + 1, len(units))

    # Compute next start with unit-aligned overlap (word budget).
    # No overlap past the end of the input.
    if index >= len(units):
        return chunk_units, index
    if overlap > 0 and index > start:
        carry: list[str] = []
        carry_words = 0
        k = index - 1
        while k >= start and carry_words < overlap:
            carry.append(units[k])
            carry_words += count_words(units[k])
            k -= 1
        carry.reverse()
        # Ensure progress: overlap must not swallow the whole chunk.
        if len(carry) >= len(chunk_units):
            return chunk_units, index
        return chunk_units, index - len(carry)
    return chunk_units, index


def _split_oversized(
    unit: str, unit_words: int, size: int, strategy: str
) -> tuple[str, str | None]:
    """Split an oversized unit into (piece ≤ size, remainder | None).

    auto: sentence-aware; otherwise (or if a sentence still exceeds size)
    hard word split at `size`.
    """
    if strategy == "auto":
        sentences = split_sentences(unit)
        if len(sentences) > 1:
            piece_words = 0
            piece_parts: list[str] = []
            for sentence in sentences:
                sw = count_words(sentence)
                if piece_words + sw > size and piece_parts:
                    remainder = " ".join(sentences[len(piece_parts) :])
                    return " ".join(piece_parts), remainder
                piece_parts.append(sentence)
                piece_words += sw
            # all sentences fit
            return " ".join(piece_parts), None
    # hard word split (sentence strategy, paragraph strategy, or a single
    # oversized sentence in auto mode)
    words = _words(unit)
    piece = " ".join(words[:size])
    tail = words[size:]
    tail_text = " ".join(tail) if tail else None
    return piece, tail_text


# backward-compatible alias
def chunk_by_size(text: str, *, size: int = 500, overlap: int = 100) -> list[str]:
    return chunk_text(text, size=size, overlap=overlap, strategy="auto")
