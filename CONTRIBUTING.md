# Contributing to EzDistro Platform

Thanks for helping. This guide is the shortest path to a working checkout and a
mergeable change. For agent-facing conventions see [AGENTS.md](AGENTS.md); for the
system itself see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development setup

```bash
cp .env.example .env          # config is pydantic-settings (app/config.py)
.venv/bin/pip install -r requirements.txt
npm install && make css       # builds the committed app/static/app.css
make bootstrap                # seed the PocketBase schema + defaults (needs PB creds)
```

A ready `.venv` (Python 3.12) exists in the repo — prefer `.venv/bin/...` over a
global interpreter. Two independent processes are needed for a full run:

```bash
make web      # uvicorn app.main:app --reload --port 8000
make worker   # python -m app.workers.worker  (needs PB_ADMIN_EMAIL/PASSWORD)
```

Both must be launched **from the repo root**: `.env` is read from the current
working directory. In development set `ALLOW_PRIVATE_NETWORKS=1` to talk to a
localhost Qdrant/WordPress/PocketBase (the SSRF guard blocks private hosts by
default).

## Checks

```bash
make lint       # ruff check + ruff format --check (format is enforced)
make typecheck  # mypy app/  (tests are excluded)
make test       # pytest tests/ -q  (~3 min; fakes only, no live services)
make check      # lint -> typecheck -> test
```

- The full suite takes ~3 minutes (a few real-time engine tests dominate).
  Run a single file while iterating: `.venv/bin/pytest tests/test_engine.py -q`.
- `tests/test_jobs_advanced.py::test_retry_uses_jittered_backoff` is flaky
  (wall-clock bounds) — rerun it before debugging.
- `app/scripts/benchmark.py` is a heavy load test, not a fixture; don't run it
  casually.
- Tests use an in-memory fake PocketBase (`tests/fakes.py`) and fake providers, so
  no live services are required.
- **mypy + structlog:** `logger.info("evt", key=value)` trips `call-arg`. Modules
  using structlog keyword logging are whitelisted in `[[tool.mypy.overrides]]` in
  `pyproject.toml`; add new such modules there.

## Architecture rules that matter

The layout is a modular monolith; keep the layering (see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3):

- `app/domain/` — pure, I/O-free logic.
- `app/repositories/` — the only code that talks to PocketBase.
- `app/providers/` — the only code that talks to external APIs.
- `app/services/` — orchestration.
- Long-running work lives **only in job handlers**, never in request handlers.

Specifics you must not break:

- **camelCase field names.** PocketBase clients run with `auto_snake_case=False`
  (`app/pb.py`). Never "fix" field names to snake_case.
- **Providers are table-driven.** To add one: subclass the protocol in
  `app/providers/base.py` and register the class in
  `registry._load_adapters()` (`app/providers/registry.py`). No provider-specific
  branches elsewhere.
- **Job handlers self-register** with `@register_job("type")`; add the type to
  `JOB_TYPES` in `app/jobs/handlers.py` (the worker and `app/api/jobs.py` derive
  from that single list). Register the module import in `ensure_registered()`.
- **Mutations are HTMX-gated** by `require_hx` (`app/api/deps.py`) and answer with
  the helpers in `app/utils.py` (`hx_toast`, `mutation_response`, …), never raw
  JSON from UI routes.

## Schema changes

`app/scripts/bootstrap_pb.py` is the source of truth for the PocketBase schema
(currently 33 collections). After editing `COLLECTIONS`:

```bash
make bootstrap        # apply additively (delete_missing=False) + union select values
make schema-export    # regenerate pb_collections_import.json from the code
```

- `import_collections` adds new fields but PocketBase does **not** replace an
  existing select's `values`; `bootstrap_pb.main()` runs `ensure_select_values()`
  to union new options into a live DB. Without it, creating a record with a new
  option fails with `validation_invalid_value`.
- `ensure_select_values()` writes through the **raw admin API** with camelCase
  keys, never the SDK's `collections.update` (the SDK snake-cases field metadata
  and drops `autogeneratePattern` on the `id` field).

## UI / frontend

- `app/static/app.css` is a **committed** Tailwind bundle — rebuild it
  (`make css`) before committing UI changes; don't hand-edit.
- `app/static/app.js` is a committed minified bundle (htmx 2 + Alpine); don't
  hand-edit it and don't expect a JS rebuild script.
- CSS source is `app/static/css/input.css`.
- Everything (secrets in transit, error toasts, dates) routes through the existing
  helpers and the `loc_*` filters in `app/templates.py`.

## Documentation

- The `docs/` set is tracked and **served in-app at `/help/`** (auth-gated); the
  Research tab links to `/help/SEO_RESEARCH.md`.
- Update the relevant doc when behavior changes; each doc carries a
  "Documentation status / Last verified" block. `docs/SCHEMA.md` and
  `docs/ARCHITECTURE.md` must agree with `bootstrap_pb.py` and the code.

## Commits and pull requests

- Conventional-commit subjects (`feat(scope): …`, `fix(scope): …`, `docs(scope): …`,
  `refactor`, `test`, `chore`, `perf`, `build`, `security`).
- Keep changes atomic and coherent; don't mix unrelated refactors.
- Run `make check` before opening a PR.

## Security

- Never log, render, or return plaintext secrets. Integration secrets are
  Fernet-encrypted (`app/services/secrets.py`); only masked previews reach the UI.
- Keep the SSRF guard (`app/providers/http.py`) intact; `ALLOW_PRIVATE_NETWORKS`
  is a development-only escape hatch.
- All PocketBase collection API rules stay `null` (superuser-only); tenant
  isolation is enforced in `app/api/deps.py`.
- See [SECURITY.md](SECURITY.md) for reporting and the security model.
