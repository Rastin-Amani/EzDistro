"""FastAPI application factory (web process)."""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.api import (
    articles,
    auth,
    dashboard,
    images,
    jobs,
    logs,
    projects,
    research,
    workers,
    workspace,
)
from app.config import settings
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


# auth + correlation middleware (was previously dead code)
app.add_middleware(AuthMiddleware)

# Compress text responses (HTML, CSS, JS, JSON) at the app layer. Without this
# the app serves a ~200 KB CSS bundle and 60 KB+ pages uncompressed on every
# cache-miss load. Added last so it wraps auth and compresses its responses.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# routers
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(projects.router)
app.include_router(articles.router)
app.include_router(research.router)
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
