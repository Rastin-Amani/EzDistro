/* ═══════════════════════════════════════════════════════════════════════
   Seoz — custom confirmation dialog (replaces the native browser confirm())
   -----------------------------------------------------------------------
   htmx 2 fires `htmx:confirm` on every request that carries `hx-confirm`.
   We intercept it (preventDefault → the native confirm() is never shown),
   display a Persian DaisyUI modal instead, and call `issueRequest(true)`
   when the user confirms — the original htmx request proceeds unchanged.
   The dialog markup lives in base.html (#confirm-dialog).
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var dialog = document.getElementById('confirm-dialog');
    if (!dialog) return;

    var textEl = dialog.querySelector('[data-confirm-text]');
    var okBtn = dialog.querySelector('[data-confirm-ok]');
    var cancelBtn = dialog.querySelector('[data-confirm-cancel]');
    var pending = null; // htmx's issueRequest(skipConfirmation)

    function close() {
        if (typeof dialog.close === 'function') dialog.close();
        pending = null;
    }

    document.addEventListener('htmx:confirm', function (e) {
        // Only intercept requests that actually ask for confirmation.
        if (!e.detail || typeof e.detail.issueRequest !== 'function') return;
        if (!e.detail.question) return;

        e.preventDefault(); // block the native window.confirm()
        if (textEl) textEl.textContent = e.detail.question;
        pending = e.detail.issueRequest;
        dialog.showModal();
    });

    if (okBtn) {
        okBtn.addEventListener('click', function () {
            var issue = pending;
            close();
            if (issue) issue(true); // skipConfirmation = true → proceed
        });
    }
    if (cancelBtn) cancelBtn.addEventListener('click', close);

    // Backdrop click / Esc closes the dialog without acting.
    var backdrop = dialog.querySelector('[data-confirm-backdrop]');
    if (backdrop) backdrop.addEventListener('click', close);
    dialog.addEventListener('cancel', function (e) {
        e.preventDefault();
        close();
    });
})();
