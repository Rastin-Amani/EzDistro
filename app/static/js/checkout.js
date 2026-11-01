/* Self-hosted checkout page controller.
 *
 * Talks only to our own routes: /api/checkout/:ref (three public fields) and
 * /api/checkout/:ref/options. The PayRam API key never reaches the browser.
 * States: loading → select → waiting → paid / partial / expired / error.
 */

import { qrSvg } from './qrcode.js';

const panel = document.getElementById('checkout-panel');

if (panel) {
    const mode = panel.dataset.mode === 'return' ? 'return' : 'checkout';
    const reference = panel.dataset.reference || '';
    const invoiceId = panel.dataset.invoiceId || '';
    const cardAvailable = panel.dataset.cardAvailable === 'true';
    const cardUrl = panel.dataset.cardUrl || '';
    const supportUrl = panel.dataset.supportUrl || '';
    const amount = Number(panel.dataset.amount || 0);
    const statusUrl = `/api/checkout/${encodeURIComponent(reference)}`;
    const optionsUrl = `/api/checkout/${encodeURIComponent(reference)}/options`;

    const POLL_MS = 5000;

    let options = null;
    let chosen = null;
    let expiresAt = parseTime(panel.dataset.expires || '');
    let pollTimer = null;
    let countdownTimer = null;
    let stopped = false;
    let busy = false;
    let view = 'loading';

    const esc = (value) =>
        String(value ?? '').replace(
            /[&<>"']/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
        );
    const money = (value) => '$' + Number(value || 0).toFixed(2);
    const safeHref = (value) => {
        const text = String(value || '');
        return /^(https?:\/\/|mailto:)/i.test(text) ? text : '';
    };
    const trust = document.querySelector('[data-trust-network]');

    function parseTime(value) {
        if (!value) return NaN;
        const text = String(value);
        return Date.parse(text.includes('T') ? text : text.replace(' ', 'T'));
    }

    async function api(url) {
        const response = await fetch(url, {
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { Accept: 'application/json' },
        });
        const data = await response.json().catch(() => null);
        if (!response.ok) {
            const error = new Error((data && data.error) || 'Request failed');
            error.status = response.status;
            throw error;
        }
        return data;
    }

    // -- rendering ----------------------------------------------------------
    function show(html, name = '') {
        panel.innerHTML = html;
        view = name;
    }

    function renderLoading() {
        show(`
            <div class="animate-pulse space-y-3" aria-hidden="true">
                <div class="h-3 w-24 rounded bg-base-200"></div>
                <div class="h-14 rounded-field bg-base-200"></div>
                <div class="h-14 rounded-field bg-base-200"></div>
            </div>
            <span class="sr-only">Loading payment options</span>
        `, 'loading');
    }

    function score(option) {
        let value = 0;
        if (option === chosen) value += 100;
        if (option.recommended) value += 10;
        if (option.mostUsed) value += 5;
        return value;
    }

    function renderSelect() {
        const sorted = [...(options || [])].sort((a, b) => score(b) - score(a));
        if (!sorted.length) return renderError('No payment methods are available right now.');
        show(`
            <div class="flex flex-col gap-3">
                <div class="flex items-center justify-between gap-3">
                    <p class="text-xs font-semibold">Choose a coin and network</p>
                    <span class="badge badge-soft badge-neutral badge-sm">${sorted.length}</span>
                </div>
                <div class="relative">
                <div role="radiogroup" aria-label="Coin and network" class="flex max-h-[18rem] flex-col gap-2 overflow-y-auto pe-1 pb-2">
                    ${sorted
                        .map((option) => {
                            const active = option === chosen;
                            const tag = option.recommended
                                ? '<span class="badge badge-xs badge-soft badge-primary">Recommended</span>'
                                : option.mostUsed
                                  ? '<span class="badge badge-xs badge-soft badge-neutral">Most used</span>'
                                  : '';
                            return `
                        <button type="button" role="radio" aria-checked="${active}" data-pick="${esc(option.id)}"
                                class="flex min-h-14 w-full items-center gap-3 rounded-field border px-3 py-2.5 text-start transition-colors ${
                                    active
                                        ? 'border-primary bg-primary/5 ring-1 ring-primary'
                                        : 'border-base-300 bg-base-100 hover:bg-base-200'
                                }">
                            <span class="grid size-8 flex-none place-items-center rounded-full bg-base-200 font-mono text-[10px] font-bold uppercase" aria-hidden="true">${esc(
                                (option.currency || '?').slice(0, 4)
                            )}</span>
                            <span class="min-w-0 flex-1">
                                <span class="block truncate text-sm font-semibold">${esc(option.currency)}
                                    <span class="font-normal text-base-content/55">on ${esc(option.network)}</span>
                                </span>
                                <span class="block truncate text-[11px] text-base-content/50">${esc(option.family)}</span>
                            </span>
                            ${tag}
                        </button>`;
                        })
                        .join('')}
                </div>
                <div aria-hidden="true" style="pointer-events:none;position:absolute;inset-inline:0;bottom:0;height:1.5rem;background:linear-gradient(to top,var(--color-base-100),transparent)"></div>
                </div>
                <p class="text-[11px] leading-relaxed text-base-content/50">
                    Send the exact amount shown next. The address belongs to this order.
                </p>
                ${
                    cardAvailable && safeHref(cardUrl)
                        ? `
                <div class="flex items-center gap-3 pt-1">
                    <span class="h-px flex-1 bg-base-300" aria-hidden="true"></span>
                    <span class="text-[10px] font-medium uppercase tracking-[0.073em] text-base-content/45">or</span>
                    <span class="h-px flex-1 bg-base-300" aria-hidden="true"></span>
                </div>
                <button type="button" data-card
                        class="flex w-full items-start gap-3 rounded-field border border-base-300 bg-base-100 px-3 py-3 text-start transition-colors hover:bg-base-200">
                    <span class="grid size-8 flex-none place-items-center rounded-full bg-base-200" aria-hidden="true">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor" class="size-4"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 8.25h19.5M2.25 8.25A2.25 2.25 0 0 1 4.5 6h15a2.25 2.25 0 0 1 2.25 2.25m-19.5 0v9A2.25 2.25 0 0 0 4.5 19.5h15a2.25 2.25 0 0 0 2.25-2.25v-9" /></svg>
                    </span>
                    <span class="min-w-0 flex-1">
                        <span class="flex flex-wrap items-center gap-1.5">
                            <span class="text-sm font-semibold">Pay with card / Apple Pay / bank</span>
                            <span class="badge badge-xs badge-soft badge-accent">Instant</span>
                        </span>
                        <span class="mt-0.5 block text-[11px] leading-relaxed text-base-content/50">Card payments may include provider fees. You'll need a one-time identity check.</span>
                    </span>
                </button>`
                        : ''
                }
            </div>
        `, 'select');
    }

    function renderWaiting() {
        if (!chosen) return renderSelect();
        if (trust) trust.textContent = `${chosen.network}`;
        let qr = '';
        try {
            qr = qrSvg(chosen.address || '');
        } catch (error) {
            qr = '';
        }
        show(`
            <div class="flex flex-col gap-4">
                <div class="flex items-center justify-between gap-3">
                    <p class="flex items-center gap-2 text-sm font-semibold">
                        <span class="relative flex size-2.5" aria-hidden="true">
                            <span class="absolute inline-flex size-full animate-ping rounded-full bg-warning opacity-60"></span>
                            <span class="relative inline-flex size-2.5 rounded-full bg-warning"></span>
                        </span>
                        Waiting for payment
                    </p>
                    <span class="badge badge-sm badge-soft badge-warning">${esc(chosen.network)}</span>
                </div>
                ${
                    qr
                        ? `<div class="flex justify-center">
                             <div class="rounded-field bg-white p-2.5 ring-1 ring-base-300">${qr}</div>
                           </div>`
                        : ''
                }
                <div class="rounded-field border border-base-300 bg-base-200/50 px-3 py-2.5">
                    <p class="text-[11px] font-medium uppercase tracking-[0.073em] text-base-content/55">Send exactly</p>
                    <p class="mt-0.5 text-xl font-extrabold tabular-nums">${money(amount)}</p>
                    <p class="text-[11px] text-base-content/55">in ${esc(chosen.currency)} on ${esc(chosen.network)}</p>
                </div>
                <div class="min-w-0">
                    <p class="text-[11px] font-medium uppercase tracking-[0.073em] text-base-content/55">Deposit address</p>
                    <code class="mt-1 block w-full rounded-field border border-base-300 bg-base-200/50 px-3 py-2.5 font-mono text-[11px] leading-5 break-all" dir="ltr">${esc(
                        chosen.address
                    )}</code>
                </div>
                <button type="button" data-copy="${esc(chosen.address)}"
                        class="btn btn-outline btn-sm min-h-11 w-full">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor" class="size-4" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H8.25m7.5 10.5h1.5a1.125 1.125 0 0 0 1.125-1.125V5.25a1.125 1.125 0 0 0-1.125-1.125h-9.75A1.125 1.125 0 0 0 6.375 5.25v.375" /></svg>
                    Copy address
                </button>
                <div class="flex items-center justify-between gap-2 border-t border-base-300 pt-3 text-xs">
                    <span class="text-base-content/55">Expires in</span>
                    <span class="whitespace-nowrap font-mono font-semibold tabular-nums" data-countdown>--:--</span>
                </div>
                <button type="button" data-change class="btn btn-ghost btn-sm min-h-11 w-full">Change payment method</button>
            </div>
        `, 'waiting');
        paintCountdown();
    }

    function renderCardExplainer() {
        const href = safeHref(cardUrl);
        if (!href) return renderSelect();
        show(`
            <div class="flex flex-col gap-4">
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                        <p class="text-base font-extrabold tracking-tight">Pay with card</p>
                        <p class="mt-1 text-xs leading-relaxed text-base-content/60">
                            You'll finish on PayRam's secure page. We never see your card details.
                        </p>
                    </div>
                    <span class="badge badge-sm badge-soft badge-accent">Instant</span>
                </div>
                <ol class="flex flex-col gap-2.5 text-xs leading-relaxed text-base-content/70">
                    <li class="flex gap-2.5"><span class="grid size-5 flex-none place-items-center rounded-full bg-base-200 text-[10px] font-bold">1</span><span>Create a PayRam Wallet or sign in.</span></li>
                    <li class="flex gap-2.5"><span class="grid size-5 flex-none place-items-center rounded-full bg-base-200 text-[10px] font-bold">2</span><span>Complete a one-time identity check (KYC).</span></li>
                    <li class="flex gap-2.5"><span class="grid size-5 flex-none place-items-center rounded-full bg-base-200 text-[10px] font-bold">3</span><span>Fund the payment, then come back — this same link updates.</span></li>
                </ol>
                <a href="${esc(href)}" class="btn btn-primary min-h-11 w-full">Continue to card payment</a>
                <button type="button" data-back class="btn btn-ghost btn-sm min-h-11 w-full">Back to crypto methods</button>
                <p class="text-center text-[11px] leading-relaxed text-base-content/45">
                    Card payments may include provider fees. Availability depends on your region and the provider.
                </p>
            </div>
        `, 'card');
    }

    function supportLink(extraClass = '') {
        const href = safeHref(supportUrl);
        if (!href) return '';
        return `<a href="${esc(href)}" target="_blank" rel="noopener"
                   class="btn btn-ghost btn-sm min-h-11 w-full ${extraClass}">Contact support</a>`;
    }

    function renderProcessing() {
        show(`
            <div class="flex min-h-[19rem] flex-col items-center justify-center gap-4 text-center">
                <span class="grid size-12 place-items-center rounded-full bg-info/10 text-info" aria-hidden="true">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                </span>
                <div>
                    <p class="text-lg font-extrabold tracking-tight">Payment is still processing</p>
                    <p class="mt-1 text-sm leading-relaxed text-base-content/60">
                        Card payments can take a few minutes. We'll mark this order paid as soon as the provider confirms it.
                    </p>
                    <p class="mt-2 text-xs leading-relaxed text-base-content/50">
                        You can safely close this page — confirmation happens in the background.
                    </p>
                </div>
                <button type="button" data-refresh class="btn btn-primary min-h-11 w-full">Refresh status</button>
                ${supportLink()}
                <a href="/checkout/${encodeURIComponent(reference)}" hx-boost="false"
                   class="btn btn-ghost btn-sm min-h-11 w-full">Back to checkout</a>
            </div>
        `, 'processing');
    }

    function renderPaid() {
        show(`
            <div class="flex min-h-[19rem] flex-col items-center justify-center gap-4 text-center">
                <span class="grid size-12 place-items-center rounded-full bg-success/10 text-success" aria-hidden="true">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>
                </span>
                <div>
                    <p class="text-lg font-extrabold tracking-tight">Payment received</p>
                    <p class="mt-1 text-sm leading-relaxed text-base-content/60">Thank you — your order is confirmed.</p>
                </div>
                <a href="/billing" hx-boost="false" class="btn btn-primary min-h-11 w-full">Back to billing</a>
                ${
                    invoiceId
                        ? `<a href="/billing/invoices/${encodeURIComponent(invoiceId)}" hx-boost="false"
                              class="btn btn-ghost btn-sm min-h-11 w-full">View invoice</a>`
                        : ''
                }
            </div>
        `, 'paid');
    }

    function renderPartial() {
        show(`
            <div class="flex min-h-[19rem] flex-col items-center justify-center gap-4 text-center">
                <span class="grid size-12 place-items-center rounded-full bg-warning/10 text-warning" aria-hidden="true">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m0 3.75h.008M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                </span>
                <div>
                    <p class="text-lg font-extrabold tracking-tight">Partial payment received</p>
                    <p class="mt-1 text-sm leading-relaxed text-base-content/60">
                        We received less than ${money(amount)}. Send the remaining amount to the same address, then check again.
                    </p>
                </div>
                <button type="button" data-refresh class="btn btn-primary min-h-11 w-full">Check again</button>
                <a href="/billing" hx-boost="false" class="btn btn-ghost btn-sm min-h-11 w-full">Back to billing</a>
            </div>
        `, 'partial');
    }

    function renderExpired() {
        show(`
            <div class="flex min-h-[19rem] flex-col items-center justify-center gap-4 text-center">
                <span class="grid size-12 place-items-center rounded-full bg-base-200 text-base-content/60" aria-hidden="true">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                </span>
                <div>
                    <p class="text-lg font-extrabold tracking-tight">This payment link has expired</p>
                    <p class="mt-1 text-sm leading-relaxed text-base-content/60">Start a new checkout to get a fresh address and amount.</p>
                </div>
                <a href="/billing" hx-boost="false" class="btn btn-primary min-h-11 w-full">Start a new checkout</a>
            </div>
        `, 'expired');
    }

    function renderError(message) {
        show(`
            <div class="flex min-h-[19rem] flex-col items-center justify-center gap-4 text-center">
                <span class="grid size-12 place-items-center rounded-full bg-error/10 text-error" aria-hidden="true">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m0 3.75h.008M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                </span>
                <div>
                    <p class="text-lg font-extrabold tracking-tight">We could not load this payment</p>
                    <p class="mt-1 text-sm leading-relaxed text-base-content/60">${esc(
                        message || 'Please try again in a moment.'
                    )}</p>
                </div>
                <button type="button" data-retry class="btn btn-primary min-h-11 w-full">Try again</button>
                <a href="/billing" hx-boost="false" class="btn btn-ghost btn-sm min-h-11 w-full">Back to billing</a>
            </div>
        `, 'error');
    }

    // -- state --------------------------------------------------------------
    function pause() {
        if (pollTimer) clearInterval(pollTimer);
        if (countdownTimer) clearInterval(countdownTimer);
        pollTimer = null;
        countdownTimer = null;
    }

    function stop() {
        stopped = true;
        pause();
    }

    function scheduleCountdown() {
        if (stopped) return;
        if (countdownTimer) clearInterval(countdownTimer);
        countdownTimer = setInterval(paintCountdown, 1000);
    }

    function paintCountdown() {
        const node = panel.querySelector('[data-countdown]');
        if (!node) return;
        let left = expiresAt - Date.now();
        if (!Number.isFinite(left)) {
            node.textContent = '—';
            return;
        }
        if (left <= 0) {
            node.textContent = '00:00';
            stop();
            renderExpired();
            return;
        }
        const total = Math.floor(left / 1000);
        const hours = String(Math.floor(total / 3600)).padStart(2, '0');
        const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
        const seconds = String(total % 60).padStart(2, '0');
        node.textContent = `${hours}:${minutes}:${seconds}`;
    }

    function schedulePoll() {
        if (pollTimer || stopped) return;
        pollTimer = setInterval(pollOnce, POLL_MS);
    }

    async function pollOnce() {
        if (busy || stopped) return;
        busy = true;
        try {
            const status = await api(statusUrl);
            if (!stopped) applyStatus(status);
        } catch (error) {
            // A transient failure must not wipe out the waiting view; the next
            // poll retries. Only a hard 404 means the order is gone.
            if (error.status === 404) {
                stop();
                renderError('This order no longer exists.');
            }
        } finally {
            busy = false;
        }
    }

    function applyStatus(status) {
        if (stopped) return; // a late response must not resurrect a finished view
        if (status && status.expiresAt) expiresAt = parseTime(status.expiresAt);
        const state = String((status && status.paymentState) || '').toUpperCase();

        if (state === 'FILLED' || state === 'OVER_FILLED') {
            stop();
            renderPaid();
            return;
        }
        if (state === 'PARTIALLY_FILLED') {
            stop();
            renderPartial();
            return;
        }
        if (state === 'CANCELLED') {
            stop();
            renderExpired();
            return;
        }
        if (state === 'FAILED') {
            stop();
            renderError('The payment could not be completed.');
            return;
        }
        if (state !== 'OPEN') {
            stop();
            renderError();
            return;
        }
        if (Number.isFinite(expiresAt) && expiresAt <= Date.now()) {
            stop();
            renderExpired();
            return;
        }
        if (chosen && chosen.address) {
            schedulePoll();
            scheduleCountdown();
            if (view === 'loading') renderWaiting();
        } else if ((view === 'select' || view === 'card') && options) {
            // Already showing the picker/explainer; keep polling but never reset it.
            schedulePoll();
        } else {
            pause();
            loadOptions();
        }
    }

    function applyReturnStatus(status) {
        if (status && status.expiresAt) expiresAt = parseTime(status.expiresAt);
        const state = String((status && status.paymentState) || '').toUpperCase();
        if (state === 'FILLED' || state === 'OVER_FILLED') {
            stop();
            renderPaid();
            return;
        }
        if (state === 'CANCELLED') {
            stop();
            renderExpired();
            return;
        }
        if (state === 'FAILED' || state === '') {
            stop();
            renderError('The payment could not be completed.');
            return;
        }
        // OPEN or PARTIALLY_FILLED: confirmed by the provider, not paid yet.
        stop();
        renderProcessing();
    }

    async function loadOptions() {
        if (options) return renderSelect();
        renderLoading();
        try {
            options = await api(optionsUrl);
            renderSelect();
            schedulePoll();
        } catch (error) {
            stop();
            renderError(error.status === 404 ? 'This order no longer exists.' : error.message);
        }
    }

    async function refresh() {
        stopped = false;
        renderLoading();
        try {
            const status = await api(statusUrl);
            if (mode === 'return') applyReturnStatus(status);
            else applyStatus(status);
        } catch (error) {
            if (Number.isFinite(expiresAt) && expiresAt <= Date.now()) renderExpired();
            else renderError(error.message);
        }
    }

    panel.addEventListener('click', async (event) => {
        const pick = event.target.closest('[data-pick]');
        if (pick) {
            chosen = (options || []).find((option) => String(option.id) === pick.dataset.pick) || null;
            if (chosen) {
                renderWaiting();
                schedulePoll();
                scheduleCountdown();
            }
            return;
        }

        const copy = event.target.closest('[data-copy]');
        if (copy) {
            const address = copy.dataset.copy || '';
            try {
                await navigator.clipboard.writeText(address);
            } catch (error) {
                const field = document.createElement('textarea');
                field.value = address;
                document.body.appendChild(field);
                field.select();
                document.execCommand('copy');
                field.remove();
            }
            const original = copy.innerHTML;
            copy.textContent = 'Copied';
            setTimeout(() => {
                copy.innerHTML = original;
            }, 1800);
            return;
        }

        if (event.target.closest('[data-change]')) {
            pause();
            renderSelect(); // chosen stays pinned and highlighted
            return;
        }
        if (event.target.closest('[data-card]')) {
            renderCardExplainer();
            return;
        }
        if (event.target.closest('[data-back]')) {
            renderSelect();
            return;
        }
        if (event.target.closest('[data-refresh]') || event.target.closest('[data-retry]')) {
            refresh();
        }
    });

    document.addEventListener('visibilitychange', () => {
        // One refetch on return, never a burst: a background tab throttles timers
        // and the counter would otherwise be stale. The return page has a manual
        // refresh instead of polling, so it never auto-checks.
        if (mode === 'checkout' && !document.hidden && !stopped) pollOnce();
    });

    window.addEventListener('pagehide', stop);

    // -- boot ---------------------------------------------------------------
    renderLoading();
    api(statusUrl)
        .then((status) => (mode === 'return' ? applyReturnStatus(status) : applyStatus(status)))
        .catch((error) => {
            if (mode === 'checkout' && Number.isFinite(expiresAt) && expiresAt <= Date.now()) {
                renderExpired();
            } else {
                renderError(error.status === 404 ? 'This order no longer exists.' : error.message);
            }
        });
}
