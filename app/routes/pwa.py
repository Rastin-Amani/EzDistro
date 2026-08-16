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
    return """<!doctype html>
<html dir="rtl">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>قطع ارتباط</title>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      body {
        font-family: system-ui, -apple-system, sans-serif;
        background: #1d232a;
        color: #ffffff;
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
        background: #ef444422;
        display: flex; align-items: center; justify-content: center;
        margin-bottom: 1.5rem;
      }
      .icon svg { width: 40px; height: 40px; stroke: #ef4444; fill: none; stroke-width: 1.5; }
      h1 { font-size: 1.25rem; font-weight: 700; margin-bottom: 0.75rem; }
      p { color: #9ca3af; line-height: 1.6; margin-bottom: 2rem; max-width: 300px; }
      .btn {
        display: inline-flex; align-items: center; gap: 0.5rem;
        padding: 0.75rem 2rem; background: #2a7eff; color: #fff;
        border: none; border-radius: 999px; font-size: 0.875rem; font-weight: 600;
        cursor: pointer; transition: opacity 0.2s;
      }
      .btn:hover { opacity: 0.85; }
      .btn:active { opacity: 0.7; }
    </style>
  </head>
  <body>
    <div class="icon">
      <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18.364 5.636a9 9 0 0 1 0 12.728m-2.829-2.829a5 5 0 0 0 0-7.07m-4.243 4.243a1 1 0 0 1 0-1.414" />
        <path d="M3 3l18 18" />
      </svg>
    </div>
    <h1>شما آفلاین هستید</h1>
    <p>صفحه‌هایی که قبلاً دیده‌اید هنوز در دسترس هستند.<br />پس از اتصال به اینترنت، دوباره امتحان کنید.</p>
    <button class="btn" onclick="window.location.reload()">تلاش مجدد</button>
  </body>
</html>"""


@router.get("/manifest.json", response_class=JSONResponse)
async def dynamic_manifest():
    manifest = {
        "name": "Fast-Htmx Boilerplate",
        "short_name": "Boilerplate",
        "description": "Fast-Htmx Boilerplate PWA",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1d232a",
        "theme_color": "#1d232a",
        "icons": [
            {
                "src": "/static/icons/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/static/icons/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return manifest


@router.get("/favicon.ico", include_in_schema=False)
async def dynamic_favicon():
    return RedirectResponse(url="/static/icons/favicon.ico")
