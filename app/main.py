"""FastAPI application factory (web process)."""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import articles, auth, dashboard, images, jobs, logs, projects, workers, workspace
from app.config import settings
from app.i18n import ENABLED_LOCALES, LOCALE_COOKIE
from app.middleware import AuthMiddleware
from app.routes import pwa
from app.templates import templates

app = FastAPI(
    title=settings.app_name,
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_prod else None,
)

templates.env.globals["app_version"] = settings.app_version
templates.env.globals["app_name"] = settings.app_name

# static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/locale/{code}", include_in_schema=False)
def switch_locale(code: str, next_url: str = "/"):
    """Persist an explicit language choice (allowlisted) and return to the page.

    GET with a cookie side effect on purpose: a UI preference, not a data
    mutation — keeps the switcher plain links (keyboard accessible, no JS).
    """
    from fastapi.responses import RedirectResponse

    if code not in ENABLED_LOCALES:
        return JSONResponse({"error": {"code": "unsupported_locale"}}, status_code=404)
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"  # open-redirect guard
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        LOCALE_COOKIE,
        code,
        max_age=365 * 24 * 3600,
        samesite="lax",
        path="/",
    )
    return response


# auth + correlation middleware (was previously dead code)
app.add_middleware(AuthMiddleware)

# routers
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(projects.router)
app.include_router(articles.router)
app.include_router(workspace.router)
app.include_router(images.router)
app.include_router(jobs.router)
app.include_router(workers.router)
app.include_router(logs.router)
app.include_router(pwa.router)

if not settings.is_prod:
    from app.routes import debug

    app.include_router(debug.router)

    @app.get("/docs", include_in_schema=False)
    def custom_docs():
        return HTMLResponse(
            """
            <!DOCTYPE html>
            <html>
            <head>
                <title>EzDistro Platform API Docs</title>
                <link rel="stylesheet" type="text/css" href="/static/swagger/swagger-ui.css">
            </head>
            <body>
                <div id="swagger-ui"></div>
                <script src="/static/swagger/swagger-ui-bundle.js"></script>
                <script src="/static/swagger/swagger-ui-standalone-preset.js"></script>
                <script>
                window.onload = function() {
                    SwaggerUIBundle({
                        url: '/openapi.json',
                        dom_id: '#swagger-ui',
                        presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
                        layout: "StandaloneLayout"
                    });
                };
                </script>
            </body>
            </html>
            """
        )


@app.get("/health", include_in_schema=False)
def health():
    """Liveness probe — reflects PocketBase reachability (degraded when down)."""
    from app.pb import get_pb

    try:
        pb = get_pb()
        pb.health.check()  # raises on failure
        pb_ok = True
    except Exception:
        pb_ok = False
    if not pb_ok:
        return JSONResponse(
            {"status": "degraded", "version": settings.app_version, "pocketbase": "unreachable"},
            status_code=503,
        )
    return JSONResponse({"status": "ok", "version": settings.app_version, "pocketbase": "ok"})
