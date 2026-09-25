# AGENTS.md

EzDistro Platform: FastAPI + HTMX (Jinja2/DaisyUI) SEO automation platform (WordPress indexer + LLM article writer). Modular monolith over PocketBase (source of truth) + Qdrant (vectors). **No Redis/Celery/n8n.**

## Two independent processes (both need a running PocketBase)

- **Web**: `make web` → `uvicorn app.main:app --reload --port 8000`
- **Worker**: `make worker` → `python -m app.workers.worker` (separate process; needs `PB_ADMIN_EMAIL`/`PB_ADMIN_PASSWORD`)
- Bootstrap the PocketBase schema (idempotent): `make bootstrap` → `python -m app.scripts.bootstrap_pb` (also seeds default prompts + admin user).

**Working directory matters**: both processes must be launched from the repo root. Config is pydantic-settings, which reads `.env` from the *current working directory* — `make web`/`make worker` already do this, but never start them from another folder. On this host, `make install-web-service` / `make install-worker-service` install systemd units (templates in `deploy/`) that run the same commands with `WorkingDirectory=__ROOT__`.

The full schema (currently **18 collections**, e.g. `article_revisions`, `provider_metrics`) is defined **as code** in `app/scripts/bootstrap_pb.py` — treat the code as the source of truth. `docs/SCHEMA.md` / `docs/ARCHITECTURE.md` are committed (the Dockerfile copies `docs/` into the image) but may lag the code, so prefer the bootstrap script when they disagree.

## Setup & env

```bash
cp .env.example .env        # config is pydantic-settings in app/config.py
.venv/bin/pip install -r requirements.txt
npm install && make css
```

- **`.env` parsing gotcha**: pydantic-settings treats everything after `=` as the value, so never put trailing comments or whitespace after a value (e.g. `SECRETS_KEY= # note` would set the key to `" # note"`). Keep each value on its own line with no inline comment.

- A ready `.venv` (Python 3.12) already exists — use `.venv/bin/...` for pytest/ruff/mypy rather than reinstalling.
- **Tests need no live services**: they run against an in-memory fake PocketBase (`tests/fakes.py`, includes unique-constraint + filter emulation) and fake providers. The full suite is ~260 tests and takes **~3 min** — a handful of real-time engine tests dominate (`test_performance.py`, `test_failure_engineering.py`, `test_production_smoke.py`), so give `make test` a long timeout. For focused work run a single file: `.venv/bin/pytest tests/test_engine.py -q`.
- `tests/test_jobs_advanced.py::test_retry_uses_jittered_backoff` is **flaky** — it asserts jittered-backoff bounds against the wall clock and intermittently fails. Rerun it before debugging if the full suite reports it.
- `app/scripts/benchmark.py` is a **heavy load-test** (real engine + handlers, fake providers, defaults of 10 concurrent jobs / 100 topics) — not a fixture; don't run casually. It is excluded from mypy.
- The SSRF guard rejects private/loopback hosts by default — set `ALLOW_PRIVATE_NETWORKS=1` for local dev against localhost Qdrant/WP/PocketBase.
- `SECRETS_KEY` (Fernet) required in prod; dev derives a deterministic key. Stored credentials are encrypted at rest (`app/services/secrets.py`) and never logged or rendered plaintext.

## Checks

```bash
make lint       # ruff check + ruff format --check  (format is enforced, not just lint)
make typecheck  # mypy app/  (tests are excluded)
make test       # pytest tests/ -q
make check      # lint -> typecheck -> test
```

**mypy gotcha**: structlog keyword logging (`logger.info("evt", key=value)`) trips `call-arg`. Modules using it are whitelisted in the `[[tool.mypy.overrides]]` block in `pyproject.toml` — if you add structlog logging in a new module, add it there too or typecheck fails.

## Architecture rules (conventions that matter)

- **Layering** (per docs/ARCHITECTURE.md): `app/domain/` is pure & I/O-free; `app/repositories/` only talks to PocketBase; `app/providers/` only talks to external APIs; `app/services/` orchestrates. In practice `app/api/` does use repositories (access control) and `projects.py` wires providers (integration health checks). Long-running work must live in **job handlers** (see next bullet), never request handlers.
- **Job handlers self-register** via `@register_job("type")` in `app/jobs/handlers.py` (imports in `ensure_registered()` trigger the side effects). Job handlers live in `app/services/` (`indexing.py`, `writing.py`, `publishing_service.py`, `retry_service.py`). The worker whitelists the 8 dispatched job types in `app/workers/worker.py` — a new job type must be added there too. Workers claim jobs atomically via unique-constrained `job_leases`; correctness never depends on worker memory.
- **Providers are table-driven**: to add a provider, subclass a protocol in `app/providers/base.py` and register the class in `registry._load_adapters()` (`app/providers/registry.py`) — it then appears in the UI dropdowns. No provider-specific branches elsewhere.
- **PocketBase clients use `auto_snake_case=False`** (`app/pb.py`) so field names stay **camelCase** end-to-end. Never "fix" them to snake_case. Admin auth falls back `_superusers` → `_admins` for PB < 0.23.

## UI / frontend

- All mutations are HTMX POST/PUT gated by `require_hx` (`app/api/deps.py`); respond with the helpers in `app/utils.py` (`hx_toast`, `mutation_response`, `ok_with_redirect`, `delayed_redirect`) — not raw JSON/redirects.
- UI is **Persian, RTL** (`lang="fa" dir="rtl"`); tests assert on Persian strings. New UI strings should be Persian; dates go through the `jalali_*` filters in `app/templates.py`.
- `app/static/app.js` is a **committed minified bundle** (htmx 2 + Alpine) — don't hand-edit it and don't expect a JS rebuild script; `package.json` only automates Tailwind. CSS source is `app/static/css/input.css`, build (`make css`) overwrites the committed `app/static/app.css` — rebuild before committing UI changes.
