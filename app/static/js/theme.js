/* ═══════════════════════════════════════════════════════════════════════
   EzDistro — adaptive light/dark theme system
   -----------------------------------------------------------------------
   * Preference persisted in localStorage key `ezdistro-theme`
      (system | light | dark). First visit / no value → light.
   * The no-flash initial paint is handled by the tiny inline script in
     base.html <head>; this file wires the switcher UI + live OS updates.
   * Survives htmx body swaps via event delegation + htmx:afterSettle.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var STORAGE_KEY = 'ezdistro-theme';
    var THEMES = { light: 'ezdistro', dark: 'ezdistro-dark' };
    var CHROME_COLORS = { light: '#ffffff', dark: '#181925' };
    var mqDark = window.matchMedia('(prefers-color-scheme: dark)');

    function savedPref() {
        try {
            var v = localStorage.getItem(STORAGE_KEY);
            return v === 'system' || v === 'light' || v === 'dark' ? v : null;
        } catch (e) {
            return null;
        }
    }

    function resolvedMode() {
        return mqDark.matches ? 'dark' : 'light';
    }

    // choice: the user's stored selection (system|light|dark); mode: actual.
    function apply(choice, persist) {
        var root = document.documentElement;
        var mode = choice === 'light' || choice === 'dark' ? choice : resolvedMode();

        var animate =
            !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (animate) root.classList.add('theme-switching');

        root.dataset.theme = THEMES[mode];
        root.dataset.themeChoice = choice;
        root.dataset.themeMode = mode;

        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', CHROME_COLORS[mode]);

        if (persist) {
            try {
                localStorage.setItem(STORAGE_KEY, choice);
            } catch (e) {
                /* private mode / quota — cosmetic only, ignore */
            }
        }

        syncUi();

        if (animate) {
            clearTimeout(apply._t);
            apply._t = setTimeout(function () {
                root.classList.remove('theme-switching');
            }, 260);
        }
    }

    // Reflect current DOM state onto every switcher instance (after htmx swaps).
    function syncUi() {
        var root = document.documentElement;
        var choice = root.dataset.themeChoice || savedPref() || 'light';
        document.querySelectorAll('[data-theme-pick]').forEach(function (input) {
            input.checked = input.getAttribute('data-theme-pick') === choice;
        });
        var mode = root.dataset.themeMode || resolvedMode();
        document.querySelectorAll('[data-theme-trigger]').forEach(function (btn) {
            btn.querySelectorAll('[data-theme-icon]').forEach(function (icon) {
                icon.classList.toggle(
                    'hidden',
                    icon.getAttribute('data-theme-icon') !== mode
                );
            });
        });
    }

    // Radio selection anywhere — native `change` bubbles from the hidden inputs
    // (works for both the dropdown and the mobile segmented control).
    document.addEventListener('change', function (e) {
        var input = e.target;
        if (!input || !input.matches || !input.matches('[data-theme-pick]')) return;
        apply(input.getAttribute('data-theme-pick'), true);
        // Close the popover dropdown (CSS :focus-within keeps it open while
        // anything inside holds focus) by releasing focus.
        if (document.activeElement && document.activeElement.blur) {
            document.activeElement.blur();
        }
    });

    // OS flips while in System mode → follow instantly. Manual overrides ignored.
    function onSystemChange() {
        var root = document.documentElement;
        var choice = root.dataset.themeChoice || savedPref() || 'light';
        if (choice === 'system') {
            apply('system', false);
        } else {
            syncUi(); // user-locked → just keep icons honest
        }
    }
    if (mqDark.addEventListener) mqDark.addEventListener('change', onSystemChange);
    else if (mqDark.addListener) mqDark.addListener(onSystemChange); // legacy Safari

    document.addEventListener('htmx:afterSettle', syncUi);
    document.addEventListener('DOMContentLoaded', syncUi);
    syncUi();
})();
