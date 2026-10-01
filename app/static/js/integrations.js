/* ═══════════════════════════════════════════════════════════════════════
   EzDistro — integrations editor (shared add/edit bottomsheet)
   -----------------------------------------------------------------------
   A single DaisyUI modal (#int-editor) is reused for creating AND editing
   every connection. Edit buttons carry a JSON payload (safe fields only —
   never the encrypted secret) in `data-payload`; Alpine fills the form via
   x-model. Registered before Alpine starts, so it survives htmx re-renders.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    function register() {
        if (window.Alpine && typeof window.Alpine.data === 'function') {
            window.Alpine.data('integrationsEditor', function () {
                return {
                open: false,
                isEdit: false,
                hasSecret: false,
                form: {
                    record_id: '',
                    category: '',
                    display_name: '',
                    provider: '',
                    model: '',
                    base_url: '',
                    username: '',
                    secret: '',
                    client_id: '',
                    redirect_uri: '',
                    api_version: '',
                    login_customer_id: '',
                    developer_token: '',
                },

                openEditor(payload, category) {
                    var self = this;
                    if (payload && payload.id) {
                        self.isEdit = true;
                        self.hasSecret = !!payload.masked;
                        self.form = {
                            record_id: payload.id || '',
                            category: payload.category || category || '',
                            display_name: payload.displayName || '',
                            provider: payload.provider || '',
                            model: payload.model || '',
                            base_url: payload.baseUrl || '',
                            username: payload.username || '',
                            secret: '',
                            client_id: payload.clientId || '',
                            redirect_uri: payload.redirectUri || '',
                            api_version: payload.apiVersion || '',
                            login_customer_id: payload.loginCustomerId || '',
                            developer_token: '',
                        };
                    } else {
                        self.isEdit = false;
                        self.hasSecret = false;
                        self.form = {
                            record_id: '',
                            category: category || '',
                            display_name: '',
                            provider: '',
                            model: '',
                            base_url: '',
                            username: '',
                            secret: '',
                            client_id: '',
                            redirect_uri: '',
                            api_version: '',
                            login_customer_id: '',
                            developer_token: '',
                        };
                    }
                    self.open = true;
                    // Read the payload off the clicked row (attribute may be a JSON string).
                    var dialog = document.getElementById('int-editor');
                    if (dialog && typeof dialog.showModal === 'function') {
                        self.$nextTick(function () {
                            dialog.showModal();
                        });
                    }
                },

                openEditorFrom(el) {
                    var payload = null;
                    if (el && el.dataset && el.dataset.payload) {
                        try {
                            payload = JSON.parse(el.dataset.payload);
                        } catch (e) {
                            payload = null;
                        }
                    }
                    this.openEditor(payload, el && el.dataset ? el.dataset.category : '');
                },

                closeEditor() {
                    this.open = false;
                    var dialog = document.getElementById('int-editor');
                    if (dialog && typeof dialog.close === 'function') dialog.close();
                },

                isPublisher() {
                    return this.form.category === 'publisher';
                },

                isGoogleAds() {
                    return this.form.category === 'google_ads';
                },
                };
            });
        }
    }

    // Alpine may already be started (or start later via app.js module) — the
    // `alpine:init` listener covers the normal case; the immediate check covers
    // pages where Alpine was already running when this file executed.
    register();
    document.addEventListener('alpine:init', register);
})();
