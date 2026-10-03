"""HTMX response helpers.

Mutation responses always return 200 OK with an empty body and the UI events
carried in the HX-Trigger header (never 204 — headers may be stripped).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.responses import Response


def hx_toast(message: str, type: str = "info") -> dict:
    """Headers dict that shows a toast AFTER the swap (safe for page replacements)."""
    event_data = {"show-toast": {"message": message, "type": type}}
    return {"HX-Trigger-After-Swap": json.dumps(event_data)}


def hx_trigger(events: dict[str, Any]) -> dict[str, str]:
    """Build HX-Trigger header value from a JSON event dictionary.

    Example: {"show-toast": {"message": "Success", "type": "success"}, "closeModal": true}
    """
    return {"HX-Trigger": json.dumps(events)}


def mutation_response(events: dict[str, Any]) -> Response:
    """200 OK, empty body, events attached — the canonical mutation response."""
    return Response(content="", headers=hx_trigger(events))


def success_response(message: str, *, extra_events: dict[str, Any] | None = None) -> Response:
    return toast_response(message, type="success", extra_events=extra_events)


def toast_response(
    message: str,
    *,
    type: str = "success",
    extra_events: dict[str, Any] | None = None,
) -> Response:
    """Toast with an arbitrary type + optional extra events (e.g. refresh list)."""
    events: dict[str, Any] = {"show-toast": {"message": message, "type": type}}
    if extra_events:
        events.update(extra_events)
    return mutation_response(events)


def error_response(message: str, *, extra_events: dict[str, Any] | None = None) -> Response:
    events: dict[str, Any] = {"show-toast": {"message": message, "type": "error"}}
    if extra_events:
        events.update(extra_events)
    return mutation_response(events)


def delayed_redirect(url: str) -> dict[str, Any]:
    """Event for the delayed-redirect listener (toast shows first, then navigate)."""
    return {"delayed-redirect": {"url": url}}


def ok_with_redirect(message: str, url: str, *, type: str = "success") -> Response:
    """Toast + delayed navigation for successful form submissions."""
    return mutation_response(
        {
            "show-toast": {"message": message, "type": type},
            "delayed-redirect": {"url": url},
        }
    )


def humanize_error(exc: Exception, fallback: str) -> str:
    """Turn an exception into a short, actionable sentence for the user.

    PocketBase validation errors become "Please fix: <field>: <reason>".
    Expected domain errors (ValueError, PermanentError) surface their message.
    Anything else falls back to the route's generic sentence — never a traceback.
    """
    import re

    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        details = data.get("data")
        parts: list[str] = []
        if isinstance(details, dict):
            for field, detail in details.items():
                if isinstance(detail, dict):
                    reason = detail.get("message") or detail.get("code") or "invalid"
                else:
                    reason = str(detail)
                label = re.sub(r"(?<!^)(?=[A-Z])", " ", str(field)).strip().lower()
                parts.append(f"{label}: {reason}")
        if parts:
            return "Please fix " + "; ".join(parts) + "."
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    if type(exc).__name__ == "PermanentError" or isinstance(exc, ValueError):
        text = str(exc).strip()
        if text:
            return text
    return fallback
