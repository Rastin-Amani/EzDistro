# fast api imports
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import structlog

from .routes import debug
from .routes import pwa
from .templates import templates

# logging config
try:
    from app.logging_config import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)
    logger.warning("structlog not available — falling back to stdlib logging")

# middleware import

# swagger/docs only in dev
IS_PROD = os.getenv("ENV", "dev").lower() == "production"

app = FastAPI(
    title="Fast-Htmx Boilerplate",
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if not IS_PROD else None,
)

APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
templates.env.globals["app_version"] = APP_VERSION

# static folder
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# middleware

# include routers
if not IS_PROD:
    app.include_router(debug.router)
app.include_router(pwa.router)


# swagger ui
if not IS_PROD:

    @app.get("/docs", include_in_schema=False)
    def custom_docs():
        return HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Fast-Htmx Boilerplate API Docs</title>
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
                    presets: [
                        SwaggerUIBundle.presets.apis,
                        SwaggerUIStandalonePreset
                    ],
                    layout: "StandaloneLayout"
                });
            };
            </script>
        </body>
        </html>
        """)
