// EzDistro Service Worker — Workbox 5.1.2, local modules
importScripts('/static/js/workbox-sw.js');

workbox.setConfig({
    modulePathPrefix: '/static/js/',
    debug: false,
});

// ---- Cache names (bumped on app version change) ----
// __CACHE_VERSION__ is replaced at runtime by app/routes/pwa.py with APP_VERSION from main.py
const CACHE_VERSION = '__CACHE_VERSION__';
const CACHE_PREFIX = `ezdistro-${CACHE_VERSION}`;
const PAGE_CACHE = `${CACHE_PREFIX}-pages`;
const STATIC_CACHE = `${CACHE_PREFIX}-static`;
const IMAGE_CACHE = `${CACHE_PREFIX}-images`;
const OFFLINE_CACHE = `${CACHE_PREFIX}-offline`;

// ---- Static assets (CSS, JS, fonts, json): NetworkFirst + expiry ----
// NetworkFirst: never run stale code after a deploy (SWR served old JS/CSS
// against new HTML and broke the UI until a second reload). Cache still
// covers offline; online loads revalidate via ETag (cheap 304s).
workbox.routing.registerRoute(
    /\.(css|js|woff2?|json)(\?.*)?$/,
    new workbox.strategies.NetworkFirst({
        cacheName: STATIC_CACHE,
        plugins: [
            new workbox.expiration.ExpirationPlugin({
                maxEntries: 60,
                maxAgeSeconds: 30 * 24 * 60 * 60,
            }),
        ],
    })
);

// ---- Install: skip waiting immediately ----
self.addEventListener('install', (event) => {
    event.waitUntil(self.skipWaiting());
});

// ---- Activate: claim all clients + purge old caches ----
self.addEventListener('activate', (event) => {
    const keep = [PAGE_CACHE, STATIC_CACHE, IMAGE_CACHE, OFFLINE_CACHE];
    event.waitUntil(
        caches
            .keys()
            .then((keys) =>
                Promise.all(
                    keys
                        .filter((k) => !keep.some((c) => k.startsWith(c)))
                        .map((k) => caches.delete(k))
                )
            )
            .then(() => self.clients.claim())
    );
});

// ---- Fetch handler: pages + images (same & cross origin) ----
self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);

    // Skip SW itself
    if (url.pathname === '/sw.js') return;
    // Skip Workbox JS modules
    if (url.pathname.startsWith('/js/workbox')) return;
    // Skip same-origin CSS/JS/fonts (handled by Workbox above)
    if (url.origin === self.location.origin && /\.(css|js|woff2?|json)(\?.*)?$/.test(url.pathname))
        return;

    // ---- Image caching: same-origin (under /static/) AND cross-origin (PocketBase) ----
    if (/\.(png|ico|svg|jpg|jpeg|gif|webp|avif)(\?.*)?$/i.test(url.pathname)) {
        event.respondWith(
            caches.match(request).then((cached) => {
                if (cached) return cached;
                return fetch(request).then((response) => {
                    if (response && response.ok) {
                        const clone = response.clone();
                        caches.open(IMAGE_CACHE).then((cache) => cache.put(request, clone));
                    }
                    return response;
                });
            })
        );
        return;
    }

    // ---- Page/HTML request: NetworkFirst with offline fallback ----
    event.respondWith(
        fetch(request)
            .then((response) => {
                if (response && response.status === 200) {
                    const clone = response.clone();
                    caches.open(PAGE_CACHE).then((cache) => cache.put(request, clone));
                }
                return response;
            })
            .catch(async () => {
                const cache = await caches.open(PAGE_CACHE);
                const cached = await cache.match(request);
                if (cached) return cached;

                const offlineCache = await caches.open(OFFLINE_CACHE);
                const offlinePage = await offlineCache.match('/offline/');
                if (offlinePage) return offlinePage;

                return new Response(
                    '<!doctype html><html dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>\u0642\u0637\u0639 \u0627\u0631\u062a\u0628\u0627\u0637</title></head><body style="font-family:system-ui;background:#1d232a;color:#fff;min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:2rem"><h1>\u0634\u0645\u0627 \u0622\u0641\u0644\u0627\u06cc\u0646 \u0647\u0633\u062a\u06cc\u062f</h1><p style="color:#9ca3af;margin:1rem 0 2rem">\u0635\u0641\u062d\u0647\u200c\u0627\u06cc \u06a9\u0647 \u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u06cc\u062f \u0628\u0627\u0632\u062f\u06cc\u062f \u06a9\u0646\u06cc\u062f \u0647\u0646\u0648\u0632 \u0630\u062e\u06cc\u0631\u0647 \u0646\u0634\u062f\u0647 \u0627\u0633\u062a.</p><button onclick="location.reload()" style="padding:.75rem 2rem;background:#2a7eff;color:#fff;border:none;border-radius:999px;font-weight:600;cursor:pointer">\u062a\u0644\u0627\u0634 \u0645\u062c\u062f\u062f</button></body></html>',
                    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                );
            })
    );
});
