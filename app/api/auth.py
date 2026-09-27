"""Authentication — PocketBase users collection, pb_auth cookie, HTMX login flow."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pocketbase.errors import ClientResponseError

from app.api.deps import current_user, is_disabled
from app.i18n import _
from app.logging_config import logger
from app.pb import get_pb
from app.templates import templates
from app.utils import error_response, ok_with_redirect

router = APIRouter()


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
def index(request: Request):
    if current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "title": _("Sign in to EzDistro"),
            "error": None,
            # Middleware sends disabled accounts here (18-A(c)).
            "disabled": request.query_params.get("disabled") == "1",
        },
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
):
    # Local client: request.state.pb is a shared superuser client (18-A(b)) —
    # authenticating a session on it would clobber the admin auth store.
    try:
        pb = get_pb()
        pb.collection("users").auth_with_password(email.strip(), password)
    except ClientResponseError as exc:
        if exc.status not in {400, 401}:
            logger.exception("auth.login_pocketbase_unavailable")
            message = _("Could not reach the server. Please try again.")
            if request.headers.get("HX-Request"):
                response = error_response(message)
                response.status_code = 503
                return response
            return _login_error(request, message, email, status_code=503)
        # Legacy HTMX login forms (still cached by the service worker) send
        # HX-Request — give them a toast. Native form POSTs (the default now)
        # get the full error page re-rendered.
        if request.headers.get("HX-Request"):
            return error_response(_("Wrong email or password"))
        return _login_error(request, email=email)

    from app.config import settings

    if is_disabled(pb.auth_store.model):
        # 18-A(c): refuse at the credential boundary too — a disabled account
        # must never receive a fresh session cookie.
        pb.auth_store.clear()
        message = _("Your account is disabled")
        if request.headers.get("HX-Request"):
            return error_response(message)
        return _login_error(request, message, email)

    token = pb.auth_store.token
    if request.headers.get("HX-Request"):
        response = ok_with_redirect(_("Welcome"), "/dashboard?welcome=1")
    else:
        # Native form POST → server-side 303. Setting the session cookie on a
        # full-page POST/redirect (a user-gesture top-level navigation) is far
        # more reliable on mobile than a Set-Cookie on an XHR response — iOS
        # Safari's ITP silently drops script-set cookies, so login used to
        # "succeed" (welcome toast) yet bounce straight back to /login.
        response = RedirectResponse(url="/dashboard?welcome=1", status_code=303)
    # Secure in production or whenever the request arrived over TLS — never let
    # the session cookie transit in cleartext (see HIGH H4).
    secure = settings.is_prod or request.url.scheme == "https"
    response.set_cookie(
        "pb_auth",
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    return response


def _login_error(
    request: Request,
    message: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> Response:
    html = templates.get_template("auth/login.html").render(
        request=request,
        title=_("Sign in to EzDistro"),
        error=message or _("Wrong email or password"),
        disabled=False,
        email=email,
    )
    return HTMLResponse(html, status_code=status_code)


@router.post("/logout")
def logout(request: Request):
    response = ok_with_redirect(_("Signed out"), "/login", type="info")
    response.delete_cookie("pb_auth")
    return response
