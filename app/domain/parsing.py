"""Robust JSON extraction from LLM output — never trust the model blindly."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract the first valid JSON document from an LLM response.

    Handles markdown fences, surrounding prose and nested braces.
    Raises ValueError when no valid JSON can be found.
    """
    if not text:
        raise ValueError("empty model response")

    candidates: list[str] = []

    # 1. fenced blocks
    for fence in _FENCE_RE.findall(text):
        candidates.append(fence.strip())

    # 2. structural regions — try the array first when the payload starts with '['
    stripped = text.lstrip()
    starts_with_array = stripped.startswith("[")
    regions = (("[", "]"), ("{", "}")) if starts_with_array else (("{", "}"), ("[", "]"))
    for open_ch, close_ch in regions:
        start = text.find(open_ch)
        while start != -1 and len(candidates) < 8:
            end = _find_balanced(text, start, open_ch, close_ch)
            if end != -1:
                candidates.append(text[start : end + 1])
            start = text.find(open_ch, start + 1)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError(f"no valid JSON found in model response (len={len(text)})")


def _find_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1
