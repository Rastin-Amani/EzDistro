/* ═══════════════════════════════════════════════════════════════════════
   EzDistro — custom confirmation dialog (replaces the native browser confirm())
   -----------------------------------------------------------------------
   htmx 2 fires `htmx:confirm` on every request that carries `hx-confirm`.
   We intercept it (preventDefault → the native confirm() is never shown),
   display a DaisyUI modal instead, and call `issueRequest(true)`
   when the user confirms — the original htmx request proceeds unchanged.
   The dialog markup lives in base.html (#confirm-dialog).

   The dialog node is looked up per interaction and the buttons are wired
   through document-level delegation: HTMX navigation only swaps
   #main-content, but a history restore (back/forward) replaces the whole
   body snapshot, which would detach any element captured at load time.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var pending = null; // htmx's issueRequest(skipConfirmation)

    function dialog() {
        return document.getElementById('confirm-dialog');
    }

    function close() {
        var el = dialog();
        if (el && typeof el.close === 'function') el.close();
        pending = null;
    }

    document.addEventListener('htmx:confirm', function (e) {
        // Only intercept requests that actually ask for confirmation.
        if (!e.detail || typeof e.detail.issueRequest !== 'function') return;
        if (!e.detail.question) return;
        var el = dialog();
        if (!el || typeof el.showModal !== 'function') return;

        e.preventDefault(); // block the native window.confirm()
        var textEl = el.querySelector('[data-confirm-text]');
        if (textEl) textEl.textContent = e.detail.question;
        pending = e.detail.issueRequest;
        el.showModal();
    });

    document.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || !target.closest) return;
        if (target.closest('[data-confirm-ok]')) {
            var issue = pending;
            close();
            if (issue) issue(true); // skipConfirmation = true → proceed
        } else if (target.closest('[data-confirm-cancel]') || target.closest('[data-confirm-backdrop]')) {
            close();
        }
    });

    // Esc closes the dialog natively; just drop the pending request.
    document.addEventListener('cancel', function () { pending = null; }, true);
})();
