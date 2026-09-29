from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

router = APIRouter()

# Path to SW file relative to project root
SW_PATH = Path(__file__).resolve().parent.parent / "static" / "sw.js"


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    from app.config import settings

    content = SW_PATH.read_text(encoding="utf-8")
    # Inject app version into CACHE_VERSION so caches auto-purge on deploy
    content = content.replace("__CACHE_VERSION__", settings.app_version)
    return Response(
        content=content,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/offline/", include_in_schema=False, response_class=HTMLResponse)
async def offline_page():
    lang, direction = "en", "ltr"
    title, heading = "You're offline — EzDistro", "You're offline"
    message, retry = (
        "Pages you've already opened may still be available.<br />Reconnect and try again.",
        "Try again",
    )

    page = """<!doctype html>
<html lang="__LANG__" dir="__DIRECTION__">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#ffffff" />
    <title>__TITLE__</title>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      body {
        font-family: system-ui, -apple-system, sans-serif;
        background: #ffffff;
        color: #181925;
        min-height: 100dvh;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 2rem;
        text-align: center;
      }
      .icon {
        width: 80px; height: 80px; border-radius: 50%;
        background: #fafafa;
        border: 1px solid #e8e8e8;
        display: flex; align-items: center; justify-content: center;
        margin-bottom: 1.5rem;
      }
      .icon svg { width: 40px; height: 40px; stroke: #666666; fill: none; stroke-width: 1.5; }
      h1 { font-size: 1.25rem; font-weight: 700; margin-bottom: 0.75rem; }
      p { color: #666666; line-height: 1.6; margin-bottom: 2rem; max-width: 300px; }
      .btn {
        display: inline-flex; align-items: center; gap: 0.5rem;
        padding: 0.75rem 2rem; background: #918df6; color: #181925;
        border: none; border-radius: 999px; font-size: 0.875rem; font-weight: 600;
        cursor: pointer; transition: opacity 0.2s;
      }
      .btn:hover { opacity: 0.85; }
      .btn:active { opacity: 0.7; }
      .btn:focus-visible { outline: 2px solid #181925; outline-offset: 3px; }
    </style>
  </head>
  <body>
    <div class="icon">
      <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18.364 5.636a9 9 0 0 1 0 12.728m-2.829-2.829a5 5 0 0 0 0-7.07m-4.243 4.243a1 1 0 0 1 0-1.414" />
        <path d="M3 3l18 18" />
      </svg>
    </div>
    <h1>__HEADING__</h1>
    <p>__MESSAGE__</p>
    <button class="btn" onclick="window.location.reload()">__RETRY__</button>
  </body>
</html>"""
    return (
        page.replace("__LANG__", lang)
        .replace("__DIRECTION__", direction)
        .replace("__TITLE__", title)
        .replace("__HEADING__", heading)
        .replace("__MESSAGE__", message)
        .replace("__RETRY__", retry)
    )


@router.get("/manifest.json", response_class=JSONResponse)
async def dynamic_manifest():
    manifest = {
        "name": "EzDistro",
        "short_name": "EzDistro",
        "description": "SEO research, article generation, and publishing workspace.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {
                "src": "/static/favicon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
        ],
    }
    return manifest


@router.get("/favicon.ico", include_in_schema=False)
async def dynamic_favicon():
    return RedirectResponse(url="/static/favicon.svg")
