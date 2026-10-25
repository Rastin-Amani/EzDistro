/* Umami product events.
 *
 * Pageviews are the tracker's job: it patches history.pushState, so every HTMX
 * boosted navigation is counted once. This file only adds the milestones that
 * answer a product question, and it must never break the page when the tracker
 * is blocked or absent.
 *
 * Contract:
 *   - HTMX mutations return an `analytics-event` HX-Trigger entry: {name, data}
 *   - full-page templates push to `window.ezAnalyticsEvents` before this loads
 */
(function () {
    function track(name, data) {
        try {
            if (!name || !window.umami || typeof window.umami.track !== 'function') return;
            if (data && Object.keys(data).length) window.umami.track(name, data);
            else window.umami.track(name);
        } catch (e) {
            /* Analytics is best-effort: a blocked script must not surface. */
        }
    }

    // A brand-new OAuth account: base.html has already stripped ?welcome=1 and
    // flagged it before the tracker captured the URL.
    if (window.ezSignupCompleted) track('signup_completed');

    document.addEventListener('analytics-event', function (event) {
        var detail = event.detail || {};
        track(detail.name, detail.data);
    });

    (window.ezAnalyticsEvents || []).forEach(function (event) {
        track(event.name, event.data);
    });
})();
