"""Shared FastAPI dependencies: auth, project access, HTMX guards, safe form casting."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.repositories.base import record_to_dict
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo


# ---------------------------------------------------------------------------
# HTMX guards
# ---------------------------------------------------------------------------
def is_hx_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def require_hx(request: Request) -> None:
    """Reject non-HTMX mutations (CSRF-lite): raises if HX-Request is absent."""
    if not is_hx_request(request):
        raise PermissionError("This endpoint only accepts HTMX requests")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
_SCOPE_UNSET: Any = object()


def current_user(request: Request) -> dict[str, Any] | None:
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    return record_to_dict(user)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise PermissionError("Authentication required")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if getattr(user, "get", lambda k, d=None: d)("role", "user") != "admin":
        raise PermissionError("Admin role required")
    return user


def is_disabled(user: Any) -> bool:
    """True when the users.disabled flag is set (18-A(c)).

    Accepts both a pocketbase ``Record`` (``auth_store.model``) and a plain
    dict (``record_to_dict`` output / test fakes).
    """
    if user is None:
        return False
    if isinstance(user, dict):
        return bool(user.get("disabled"))
    return bool(getattr(user, "disabled", False))


def project_scope(request: Request) -> list[str] | None:
    """Project ids visible to the current user.

    Returns None for global admins (unrestricted); otherwise the list of
    project ids the user belongs to — which may be EMPTY, meaning the user has
    NO access anywhere. Treating [] as "unrestricted" was an auth bypass
    (a user with zero memberships could reach every project).

    Memoized per request: routes call this several times (often once per
    helper), and each non-admin call is a PocketBase query.
    """
    cached = getattr(request.state, "project_scope_cache", _SCOPE_UNSET)
    if cached is not _SCOPE_UNSET:
        return cached
    user = require_user(request)
    if user.get("role") == "admin":
        result: list[str] | None = None
    else:
        memberships = MemberRepo(request.state.pb).list_for_user(user.get("id", ""))
        result = [str(m["project"]) for m in memberships if m.get("project")]
    request.state.project_scope_cache = result
    return result


def can_access_project(request: Request, project_id: str) -> bool:
    scope = project_scope(request)
    return scope is None or project_id in scope


def require_project_access(request: Request, project_id: str) -> dict[str, Any]:
    """Validate project exists AND the user may access it; return the project record."""
    if not can_access_project(request, project_id):
        raise PermissionError("You do not have access to this project")
    project = ProjectRepo(request.state.pb).get(project_id)
    if not project:
        raise ValueError("Project not found")
    return project


# Roles with full mutation rights on a project (owner > admin > editor > viewer).
PROJECT_WRITE_ROLES = ("owner", "admin", "editor")
PROJECT_ADMIN_ROLES = ("owner", "admin")  # destructive / administrative ops


def require_project_role(
    request: Request, project_id: str, roles: tuple[str, ...] = PROJECT_WRITE_ROLES
) -> str:
    """Enforce the member's role within a project (project-level authorization).

    Global admins (users.role == "admin") bypass project-role checks. Non-members
    are rejected by require_project_access.
    """
    user = require_user(request)
    if user.get("role") == "admin":
        return "admin"
    require_project_access(request, project_id)
    role = MemberRepo(request.state.pb).role_of(project_id, user.get("id", ""))
    if role not in roles:
        raise PermissionError("Insufficient project role")
    return role


def ensure_record_in_project(
    record: dict[str, Any] | None, project_id: str, label: str = "record"
) -> None:
    """Project-isolation guard for entity IDs: reject records from another project.

    MUST be called after require_project_access() on every route that mutates a
    record addressed by raw id — otherwise a member of project A could read or
    modify records of project B by substituting ids (see CRITICAL C1).
    """
    if record is None or str(record.get("project") or "") != str(project_id):
        raise ValueError(f"{label} not found")


# ---------------------------------------------------------------------------
# Bulletproof form parsing (anti-422)
# ---------------------------------------------------------------------------
def safe_int(value: str | int | None, default: int = 0) -> int:
    """Cast form values to int without ever raising (HTML forms send empty strings)."""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def safe_float(value: str | float | None, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def safe_str(value: str | None, default: str = "") -> str:
    if value is None:
        return default
    return value.strip()


# ---------------------------------------------------------------------------
# Auth guards used by routes
# ---------------------------------------------------------------------------
def login_redirect(request: Request) -> RedirectResponse:
    """Where to send unauthenticated visitors."""
    return RedirectResponse(url="/login", status_code=303)


# Re-export for route modules that want a tiny API surface.
__all__ = [
    "is_hx_request",
    "require_hx",
    "current_user",
    "require_user",
    "require_admin",
    "is_disabled",
    "project_scope",
    "can_access_project",
    "require_project_access",
    "require_project_role",
    "PROJECT_WRITE_ROLES",
    "PROJECT_ADMIN_ROLES",
    "ensure_record_in_project",
    "safe_int",
    "safe_float",
    "safe_bool",
    "safe_str",
    "login_redirect",
]
