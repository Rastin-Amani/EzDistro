/* ═══════════════════════════════════════════════════════════════════════
   EzDistro — provider / model convenience wiring
   -----------------------------------------------------------------------
   When a provider (connection) is picked from a <select>, its saved default
   model is copied into the sibling model field. The embedding model also
   auto-fills its output dimensions from a small known-model table (the
   provider stays the source of truth for unknown ids).

   Declarative contract:
     <select data-provider-model="{target selector}"> … <option data-model="…">
     <input data-embedding-model> + <input data-embedding-dims>
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    // Kept in sync with app/providers/registry.py EMBEDDING_MODEL_DIMENSIONS.
    var EMBED_DIMS = {
        'text-embedding-3-small': 1536,
        'text-embedding-3-large': 3072,
        'text-embedding-ada-002': 1536,
        'text-embedding-004': 768,
        'gemini-embedding-001': 3072,
        'embedding-001': 768,
        'nomic-embed-text': 768,
        'mxbai-embed-large': 1024,
        'all-minilm': 384,
        'bge-large-en': 1024,
        'bge-base-en': 768,
        'bge-small-en': 384,
        'e5-large': 1024,
        'e5-base': 768,
        'e5-small': 384,
        'multilingual-e5-large': 1024,
    };

    function selectedModel(select) {
        var opt = select.options[select.selectedIndex];
        return opt && opt.dataset ? (opt.dataset.model || '') : '';
    }

    function applyProviderModel(select, overwrite) {
        var targetSel = select.getAttribute('data-provider-model');
        if (!targetSel) return;
        var target = document.querySelector(targetSel);
        if (!target) return;
        var model = selectedModel(select);
        if (model && (overwrite || !target.value)) target.value = model;
    }

    function applyEmbeddingDims(input, overwrite) {
        var target = document.querySelector(input.getAttribute('data-embedding-dims') || '#embedding_dimensions');
        if (!target) return;
        var dims = EMBED_DIMS[String(input.value || '').trim().toLowerCase()];
        if (dims && (overwrite || !target.value || Number(target.value) === 0)) {
            target.value = dims;
        }
    }

    function wire(root) {
        var scope = root && root.querySelectorAll ? root : document;

        scope.querySelectorAll('[data-provider-model]').forEach(function (select) {
            if (select.dataset.ezdBound) return;
            select.dataset.ezdBound = '1';
            applyProviderModel(select, false);
            select.addEventListener('change', function () {
                applyProviderModel(select, true);
            });
        });

        scope.querySelectorAll('[data-embedding-model]').forEach(function (input) {
            if (input.dataset.ezdBound) return;
            input.dataset.ezdBound = '1';
            applyEmbeddingDims(input, false);
            var handler = function () {
                applyEmbeddingDims(input, true);
            };
            input.addEventListener('change', handler);
            input.addEventListener('input', handler);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            wire(document);
        });
    } else {
        wire(document);
    }
    // htmx swaps replace form markup; re-wire so new selects behave too.
    document.addEventListener('htmx:afterSwap', function (evt) {
        wire(evt.target);
    });
})();
