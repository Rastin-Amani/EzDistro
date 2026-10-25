"""Authentication — PocketBase OAuth2 (Google / GitHub) and the pb_auth cookie.

There is no password sign-in: PocketBase's users collection only allows the
Google and GitHub OAuth2 providers. Sign-in and sign-up are the same action
(the provider creates the account on first use and links an existing record by
verified email), so there is a single page; `/signup` redirects to it.

Flow (PocketBase "manual code exchange"):
1. `GET /login` asks PocketBase for the enabled providers (`auth-methods`).
   Each provider carries a one-off `state` + PKCE `codeVerifier`; we append our
   callback URL to its `authURL` and keep the pair in a signed, HttpOnly,
   10-minute cookie.
2. The provider redirects back to `/auth/oauth2/callback?code=…&state=…`.
3. We match `state` against the cookie and exchange the code with PocketBase
   (`auth-with-oauth2`), then store the returned token in the same `pb_auth`
   cookie the middleware already validates.

The OAuth callback is a top-level browser navigation, so failures redirect with
`?error=<code>` (never a JSON body) and the login page renders the message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pocketbase.errors import ClientResponseError

from app.api.deps import current_user, is_disabled
from app.config import settings
from app.logging_config import logger
from app.pb import get_pb
from app.templates import templates
from app.utils import ok_with_redirect

router = APIRouter()

# Only these providers are offered, even if PocketBase has others enabled.
ALLOWED_PROVIDERS = ("google", "github")
PROVIDER_LABELS = {"google": "Google", "github": "GitHub"}

OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_TTL = 10 * 60  # seconds; plenty for a provider login round trip
SESSION_MAX_AGE = 60 * 60 * 24 * 30

ERROR_MESSAGES: dict[str, tuple[str, str]] = {
    "cancelled": ("Sign-in was cancelled.", "warning"),
    "state": ("That sign-in link expired. Please try again.", "warning"),
    "disabled": (
        "Your account has been deactivated. Contact an administrator to reactivate it.",
        "warning",
    ),
    "unavailable": ("Could not reach the sign-in service. Please try again.", "error"),
    "failed": ("We could not complete sign-in. Please try again.", "error"),
    "password": (
        "Password sign-in is no longer available. Continue with Google or GitHub.",
        "warning",
    ),
}


# ---------------------------------------------------------------------------
# Signed state cookie
# ---------------------------------------------------------------------------
def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _state_signature(body: str) -> str:
    key = settings.effective_secrets_key
    return _b64encode(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def _sign_state(payload: dict) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_state_signature(body)}"


def _read_state(request: Request) -> dict:
    """Return the verified state payload, or {} when absent/tampered/expired."""
    raw = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if "." not in raw:
        return {}
    body, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature.encode("utf-8"), _state_signature(body).encode("utf-8")):
        return {}
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        expired = int(payload.get("exp") or 0) < time.time()
    except (ValueError, TypeError):
        return {}
    if expired:
        return {}
    return payload


def _secure_cookie(request: Request) -> bool:
    return settings.is_prod or request.url.scheme == "https"


def oauth_callback_url(request: Request) -> str:
    """Absolute OAuth callback for this browser request.

    Built from the origin the user actually reached, not from PUBLIC_APP_URL:
    a stale canonical URL (e.g. the marketing domain) makes every provider
    reject the callback with `redirect_uri_mismatch`. The resulting URL must be
    registered at Google/GitHub; an unregistered one is rejected by them, so a
    spoofed Host header cannot receive an auth code.
    """
    host = request.headers.get("host", "")
    if not host:
        return settings.oauth_callback_url
    scheme = "https" if settings.is_prod or request.url.scheme == "https" else request.url.scheme
    return f"{scheme}://{host}{settings.oauth_callback_path}"


# ---------------------------------------------------------------------------
# PocketBase providers
# ---------------------------------------------------------------------------
def provider_href(auth_url: str, callback_url: str) -> str:
    """Point the provider URL at our callback (PB leaves redirect_uri blank)."""
    parts = urllib.parse.urlsplit(auth_url)
    query = {
        key: values[0]
        for key, values in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()
    }
    query["redirect_uri"] = callback_url
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def load_providers(callback_url: str) -> tuple[list[dict], str | None]:
    """Enabled Google/GitHub providers, ordered, plus an error flag for the UI."""
    try:
        methods = get_pb().send("/api/collections/users/auth-methods", {"method": "GET"})
    except Exception:
        logger.exception("auth.auth_methods_unavailable")
        return [], "unavailable"

    oauth2 = (methods or {}).get("oauth2") or {}
    if not oauth2.get("enabled"):
        # Disabled is an operator/config state, not an outage — the page shows
        # "not configured" rather than "try again later".
        return [], None

    providers: dict[str, dict] = {}
    for item in oauth2.get("providers") or []:
        name = str(item.get("name") or "")
        auth_url = str(item.get("authUrl") or item.get("authURL") or "")
        state = str(item.get("state") or "")
        verifier = str(item.get("codeVerifier") or "")
        if name in ALLOWED_PROVIDERS and auth_url and state and verifier:
            providers[name] = {
                "name": name,
                "label": PROVIDER_LABELS[name],
                "href": provider_href(auth_url, callback_url),
                "state": state,
                "verifier": verifier,
            }
    ordered = [providers[name] for name in ALLOWED_PROVIDERS if name in providers]
    return ordered, None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _render_auth(request: Request, error: str | None, status_code: int = 200):
    callback_url = oauth_callback_url(request)
    providers, problem = load_providers(callback_url)
    message, toast_type = ERROR_MESSAGES.get(error or "", (None, "error"))
    if not problem and not providers:
        # PocketBase answered, but neither provider is configured. This is an
        # operator problem, not a user one — say so plainly instead of showing
        # an empty login card.
        problem = "not_configured"

    html = templates.get_template("auth/login.html").render(
        request=request,
        title=("Sign in to EzDistro"),
        providers=providers,
        provider_text=" or ".join(p["label"] for p in providers) or "Google or GitHub",
        problem=problem,
        error=message,
        toast_type=toast_type,
    )
    response = HTMLResponse(
        html,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )

    if providers:
        # Merge into any still-valid state so two open tabs (or a back button)
        # do not invalidate each other's sign-in attempt.
        payload = _read_state(request) or {}
        entries = dict(payload.get("p") or {})
        entries.update({p["name"]: {"s": p["state"], "v": p["verifier"]} for p in providers})
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            _sign_state({"exp": int(time.time()) + OAUTH_STATE_TTL, "p": entries}),
            max_age=OAUTH_STATE_TTL,
            httponly=True,
            samesite="lax",
            secure=_secure_cookie(request),
            path="/auth",
        )
    return response


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
def index(request: Request):
    if current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    error = request.query_params.get("error") or (
        "disabled" if request.query_params.get("disabled") == "1" else None
    )
    return _render_auth(request, error)


@router.get("/signup")
def signup_page(request: Request):
    """Sign-in and sign-up are one OAuth page: the provider creates the account
    on the first login and links an existing record by verified email."""
    return RedirectResponse(url="/login", status_code=303)


@router.post("/login")
def password_login_removed(request: Request):
    """Passwords were removed. Cached/old forms land here — never 405/422."""
    target = "/login?error=password"
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(url=target, status_code=303)


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------
@router.get("/auth/oauth2/callback")
def oauth_callback(request: Request):
    params = request.query_params
    if params.get("error") or not params.get("code") or not params.get("state"):
        return _fail(request, "cancelled" if params.get("error") == "access_denied" else "failed")

    state = params.get("state", "")
    entries = _read_state(request).get("p") or {}
    provider = ""
    verifier = ""
    for name, entry in entries.items():
        if isinstance(entry, dict) and hmac.compare_digest(
            str(entry.get("s") or "").encode("utf-8"), state.encode("utf-8")
        ):
            provider = str(name)
            verifier = str(entry.get("v") or "")
            break
    if provider not in ALLOWED_PROVIDERS or not verifier:
        return _fail(request, "state")

    try:
        pb = get_pb()
        # Local client (never the shared superuser data client): authenticating
        # a user session on it would clobber the admin auth store (18-A(b)).
        # Send the documented `redirectURL` key ourselves: the pinned Python
        # SDK still posts the deprecated `redirectUrl`, which PocketBase logs a
        # warning about on every login.
        users = pb.collection("users")
        payload = pb.send(
            "/api/collections/users/auth-with-oauth2",
            {
                "method": "POST",
                "body": {
                    "provider": provider,
                    "code": params.get("code", ""),
                    "codeVerifier": verifier,
                    "redirectURL": oauth_callback_url(request),
                    "createData": {"role": "member", "emailVisibility": False},
                },
            },
        )
        result = users.auth_response(payload)
    except ClientResponseError as exc:
        if exc.status >= 500 or exc.status == 0:
            logger.exception("auth.oauth_unavailable", provider=provider)
            return _fail(request, "unavailable")
        logger.warning("auth.oauth_rejected", provider=provider, status=exc.status)
        return _fail(request, "failed")
    except Exception:
        logger.exception("auth.oauth_failed", provider=provider)
        return _fail(request, "unavailable")

    if is_disabled(pb.auth_store.model):
        # 18-A(c): a disabled account must never receive a fresh session cookie.
        pb.auth_store.clear()
        return _fail(request, "disabled")

    token = pb.auth_store.token
    if not token:
        logger.error("auth.oauth_missing_token", provider=provider)
        return _fail(request, "failed")

    is_new = bool((result.meta or {}).get("isNew")) if result else False
    logger.info("auth.oauth_success", provider=provider, is_new=is_new)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
    response.set_cookie(
        "pb_auth",
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
    )
    return response


def _fail(request: Request, error: str) -> RedirectResponse:
    """Return to the login page with a friendly code; drop the used state."""
    response = RedirectResponse(url=f"/login?error={error}", status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
    if error == "disabled":
        response.delete_cookie("pb_auth")
    return response


@router.post("/logout")
def logout(request: Request):
    if request.headers.get("HX-Request") == "true":
        response: Response = ok_with_redirect(("Signed out"), "/login", type="info")
    else:
        response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("pb_auth")
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
    return response
