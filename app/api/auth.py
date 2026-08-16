"""Authentication — PocketBase users collection, pb_auth cookie, HTMX login flow."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import current_user
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
        request, "auth/login.html", {"title": "ورود به سئوز", "error": None}
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
):
    try:
        request.state.pb.collection("users").auth_with_password(email.strip(), password)
    except Exception:
        # HTMX requests (the login form) swap nothing — give them a visible toast.
        # Plain browser POSTs (no JS) still get the full error page.
        if request.headers.get("HX-Request"):
            return error_response("ایمیل یا رمز عبور اشتباه است")
        return _login_error(request)

    from app.config import settings

    token = request.state.pb.auth_store.token
    response = ok_with_redirect("خوش آمدید", "/dashboard")
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


def _login_error(request: Request) -> Response:
    html = templates.get_template("auth/login.html").render(
        request=request, title="ورود به سئوز", error="ایمیل یا رمز عبور اشتباه است"
    )
    return HTMLResponse(html)


@router.post("/logout")
def logout(request: Request):
    response = ok_with_redirect("خارج شدید", "/login", type="info")
    response.delete_cookie("pb_auth")
    return response
