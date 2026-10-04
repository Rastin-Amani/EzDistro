/* ═══════════════════════════════════════════════════════════════════════
   EzDistro — integrations editor (shared add/edit bottomsheet)
   -----------------------------------------------------------------------
   A single DaisyUI modal (#int-editor) is reused for creating AND editing
   every connection. Edit buttons carry a JSON payload (safe fields only —
   never the encrypted secret) in `data-payload`; Alpine fills the form via
   x-model. Registered before Alpine starts, so it survives htmx re-renders.

   The provider list + field visibility come from the server (`provider_meta`)
   so the form follows the provider schema instead of a generic LLM form.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    function register() {
        if (window.Alpine && typeof window.Alpine.data === 'function') {
            window.Alpine.data('integrationsEditor', function (providerMeta, providerDefaults) {
                return {
                meta: providerMeta || {},
                defaults: providerDefaults || {},
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
                    client_secret: '',
                },

                providersFor(category) {
                    return this.meta[category] || [];
                },

                selectedMeta() {
                    var list = this.providersFor(this.form.category);
                    return list.find((p) => p.provider === this.form.provider) || {};
                },

                providerDescription() {
                    return this.selectedMeta().description || '';
                },

                baseUrlPlaceholder() {
                    return this.selectedMeta().default_base_url || 'https://api.example.com/v1';
                },

                // Categories whose provider exposes a default model.
                requiresModel() {
                    return ['llm', 'embedding', 'reranker', 'image'].indexOf(this.form.category) !== -1;
                },

                supportsModels() {
                    return this.requiresModel() && this.selectedMeta().supports_model_listing !== false;
                },

                showBaseUrl() {
                    if (this.isGoogleAds() || this.form.provider === 'none') return false;
                    if (['publisher', 'vector_store', 'serp'].indexOf(this.form.category) !== -1) return true;
                    return this.requiresModel();
                },

                showSecret() {
                    if (this.isGoogleAds() || this.form.provider === 'none') return false;
                    return true;
                },

                secretLabel() {
                    if (this.isPublisher()) return 'Application password';
                    if (this.form.category === 'vector_store') return 'API key (optional)';
                    return 'API key';
                },

                onProviderChange() {
                    // Keep the model only when the provider already carries a default;
                    // never invent one — the user can fetch/type it.
                    var meta = this.selectedMeta();
                    if (!this.requiresModel()) {
                        this.form.model = '';
                    }
                    if (meta.default_base_url && !this.isEdit) {
                        this.form.base_url = meta.default_base_url;
                    }
                },

                defaultProvider(category) {
                    return this.defaults[category] || '';
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
                            client_secret: '',
                        };
                    } else {
                        self.isEdit = false;
                        self.hasSecret = false;
                        self.form = {
                            record_id: '',
                            category: category || '',
                            display_name: '',
                            provider: self.defaultProvider(category),
                            model: '',
                            base_url: '',
                            username: '',
                            secret: '',
                            client_id: '',
                            redirect_uri: '',
                            api_version: '',
                            login_customer_id: '',
                            developer_token: '',
                            client_secret: '',
                        };
                        self.onProviderChange();
                    }
                    self.open = true;
                    // Drop stale model options from a previously opened connection.
                    var modelList = document.getElementById('int-models');
                    if (modelList) modelList.innerHTML = '';
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
