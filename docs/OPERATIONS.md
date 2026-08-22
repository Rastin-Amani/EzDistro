# Seoz Platform — Operations Runbook

Operational procedures for deploying, running, monitoring and recovering the Seoz
platform. Audience: engineers or operators responsible for a running deployment.

```text
Documentation status:  Verified against Makefile, Dockerfile, config, worker code
Last verified:         2026-08-22
```

---

## 1. Topology recap

Two independent processes share one PocketBase instance:

| Process | Command | Listens |
|---|---|---|
| Web (`make web`) | `uvicorn app.main:app --reload --port 8000` | **8000** (drop `--reload` in prod; the Docker image already runs without it and binds `0.0.0.0`) |
| Worker (`make worker`) | `python -m app.workers.worker` | none (outbound only) |

External dependencies:

| Dependency | Version / notes |
|---|---|
| PocketBase | **≥ 0.23** required (collection-import API shape); auth falls back `_superusers` → `_admins` |
| Qdrant | HTTP endpoint; default `http://127.0.0.1:6333`, overridable per project integration |
| Provider APIs | LLM / embeddings / rerank / WordPress — configured per project in the UI |

Python 3.12 venv expected (`.venv/bin/python`). Node.js is needed only to build CSS.

## 2. First-time setup

```bash
cp .env.example .env                 # then edit values (see §4)
pip install -r requirements.txt      # or: .venv/bin/pip install -r requirements.txt
npm install && make css              # builds committed app/static/app.css
```

Create/update the schema and seed defaults (idempotent — safe to re-run):

```bash
export PB_ADMIN_EMAIL=… PB_ADMIN_PASSWORD=…   # or put them in .env
make bootstrap        # python -m app.scripts.bootstrap_pb
```

Bootstrap does, in order: import/patch all 18 collections (`delete_missing=False`),
add `role`/`displayName` to `users`, seed the `app_settings` singleton + 8 global
Persian prompts, and create the platform admin if `SEED_ADMIN_PASSWORD` is set.
It prints explicit confirmation lines for each step; on failure it raises with a
hint about reachability/credentials/PB version.

An alternative artifact, `pb_collections_import.json`, mirrors the same schema for a
manual import through the PocketBase Admin UI. Keep it in sync with the code when the
schema changes.

## 3. Running

### Bare metal / VM

```bash
make web       # development: --reload on :8000
make worker    # needs PB_ADMIN_EMAIL/PASSWORD at runtime
```

Run each under your process supervisor of choice. Both processes are stateless;
all durable state lives in PocketBase/Qdrant.

Example systemd unit skeletons (adapt paths/user):

```ini
# seoz-web.service
[Service]
WorkingDirectory=/srv/seoz
Environment=ENV=production
ExecStart=/srv/seoz/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
Restart=always

# seoz-worker.service
[Service]
WorkingDirectory=/srv/seoz
Environment=ENV=production
ExecStart=/srv/seoz/.venv/bin/python -m app.workers.worker
Restart=always
```

(The exact uvicorn flags mirror the provided Dockerfile CMD.)

### Docker

The included `Dockerfile` ships the web process by default:

```bash
docker build -t seoz .
docker run --env-file .env -p 8000:8000 seoz
# worker:
docker run --env-file .env seoz python -m app.workers.worker
```

Note the image copies only `requirements.txt`, `app/` and `docs/` — `.env` must come
from the environment (`--env-file`) since it is gitignored.

### Graceful shutdown

SIGINT/SIGTERM stop the worker cleanly: loops cancel, metrics flush at shutdown,
in-flight handlers finish their current step. Jobs interrupted mid-flight are
recovered automatically by lease expiry (`LEASE_SECONDS`, default 300 s) — restarts
never require manual cleanup.

## 4. Production configuration checklist

- [ ] `ENV=production`
- [ ] `SECRETS_KEY` generated and stored safely — **required**, startup refuses
      otherwise. Losing this key makes every stored integration secret undecryptable.
      Rotating it also invalidates them (re-enter credentials afterwards).
- [ ] `PB_URL` + `PB_ADMIN_EMAIL/PASSWORD` set (worker + bootstrap need admin creds)
- [ ] Seed admin password changed after first login
- [ ] `ALLOW_PRIVATE_NETWORKS=0` (default) unless you intentionally proxy private hosts
- [ ] TLS in front of the web process — the session cookie gets its `Secure` flag
      automatically in production or on https requests
- [ ] `/docs`, `/openapi.json`, debug routes are disabled automatically in production
- [ ] Worker sizing: `MAX_CONCURRENT_JOBS=4`, `LLM_CONCURRENCY=4`,
      `EMBEDDING_CONCURRENCY=4`, `PUBLISH_CONCURRENCY=2` reviewed against provider
      rate limits
- [ ] Qdrant reachable and sized for your embedding dimensions (per project+model
      collections are created on first index run)

## 5. Health & monitoring

**Liveness probe:** `GET /health`
→ `200 {"status":"ok","version":…,"pocketbase":"ok"}` when PocketBase answers,
→ `503 {"status":"degraded","pocketbase":"unreachable"}` otherwise.
Use it for container/orchestrator health checks; it checks PB only (Qdrant/provider
health lives in per-integration status).

**Logs:** structlog to stdout. Dev (`ENV=dev`) renders console lines; anything else
renders JSON. Correlation: web requests carry `req_id`; workers log with job ids.
Uvicorn's access log is silenced in favour of request lifecycle events. Ship stdout
with your normal collector; there is no in-app file logging or rotation.

**In-app observability:**
- Dashboard (`/dashboard`) — pipeline counts, failure rate, average durations,
  indexing + provider health; aggregates cached 5 s.
- Job monitor (`/jobs`), dead letters (`/failed`), per-job event timeline with
  provider-call stats when `PROVIDER_EVENTS_ENABLED=1`.
- `provider_metrics` collection — daily per `(project, provider, model, operation)`
  rows with latency histograms (P95 computed on read), token usage. Flushed every
  30 s per worker and at shutdown.

**Retention gaps (no automated pruner exists):** `job_events`, `provider_metrics`,
`publishing_runs`, `article_revisions` grow unbounded by design (append-only audit).
Plan periodic archiving/pruning out-of-band if volume matters.

## 6. Scaling

- **Workers:** run as many as you like on one or more machines. Atomic lease claims
  (unique `job_leases.job`) make double-execution impossible; crashed workers'
  jobs are reclaimed after lease expiry.
- **Provider ceilings multiply per worker:** total concurrent LLM calls ≈
  `workers × LLM_CONCURRENCY`. Tune before scaling out against provider quotas.
- **Web:** stateless — scale horizontally behind a load balancer; sessions live in
  the signed PB cookie, not server memory.
- **PocketBase:** single-writer SQLite — one PB instance; scale reads carefully and
  keep backups current.

## 7. Upgrades & schema changes

1. Read the release notes / diff of `bootstrap_pb.py`.
2. Deploy code, then re-run `make bootstrap` — it merges collection definitions
   additively (`delete_missing=False`: collections/fields removed from code are NOT
   deleted from the database).
3. Restart web + worker. In-flight jobs survive restarts (lease recovery).
4. If embedding model/dimensions changed for a project, trigger a full reindex
   (**بازنمایه کامل**) from the UI; collections are namespaced per model so nothing
   mixes.

## 8. Backups & disaster recovery

| Asset | Backup method | Recoverability |
|---|---|---|
| PocketBase | Built-in backups or filesystem snapshot of its data dir (SQLite). **Source of truth** — projects, articles, jobs history, secrets ciphertext | Restore file, restart processes |
| `SECRETS_KEY` | Store separately from PB backups (e.g. secret manager) | Without it, encrypted credentials cannot be decrypted — re-entering them is the only recovery |
| Qdrant | Optional snapshots | Or: full reindex from WordPress (**بازنمایه کامل**) rebuilds everything deterministically from source posts + PB document metadata |
| WordPress | Out of scope (your site's own backups) | Articles always remain in PB; republishing updates existing posts |

DR drill sketch: restore PB snapshot → start web+worker with same env (incl.
`SECRETS_KEY`) → check `/health` → verify dashboard counters render → optionally
full-reindex one project into a fresh Qdrant.

## 9. Routine procedures

| Task | Procedure |
|---|---|
| Rotate a provider API key | Project → اتصالات → edit connection → save new secret → تست |
| Change publishing mode | Project settings → publishingMode `draft`/`publish` |
| Add/remove schedule | Project scheduling form (interval-based; kind index/write) — schedules only fire while the project is `active` and a worker's schedule loop runs |
| Purge stale vectors for deleted posts | Full reindex (بازنمایه کامل) — the only path that deletes unseen sources' vectors |
| Requeue stuck work | Inspect `/failed` → تلاش مجدد per job; running jobs self-heal via lease expiry |
| Kill runaway generation | وظایف → job detail → لغو (cooperative; stops at next checkpoint) |

## 10. Quick incident triage

| Symptom | First checks |
|---|---|
| Web returns redirects to login for everyone | PB reachable? `/health` 503 means auth refresh fails too — fix PB first |
| Worker idle, jobs pending | Worker process up? Its startup line lists concurrency caps; check `PB_ADMIN_EMAIL/PASSWORD`; look for `worker.poll.error` logs |
| Jobs flip to `retrying` repeatedly | Job detail error timeline names the failing provider + category (timeout/429/5xx = transient; auth/schema = permanent) |
| Index run stalls | Check the run's `lastSourceId` checkpoint and WP reachability; retry resumes from checkpoint |
| Publish failed but post exists on WP | Do NOT recreate manually — retry the publish job; orphan recovery finds the post by slug/meta and updates instead of duplicating |

Details and fixes: TROUBLESHOOTING.md · failure-mode matrix: FAILURES.md.

## 11. Known operational caveats (from the documentation audit)

1. **Unpublish flow vs schema enums** — the unpublish action writes
   `mode="unpublish"` / `status="unpublished"`, which the bootstrapped
   `publishing_runs` selects do not allow; expect validation failure until patched
   (schema migration or code fix).
2. **`generationConcurrency` setting is inert** — effective section parallelism comes
   from worker env caps only.
3. **No retention pruner** for append-only audit collections (see §5).
4. **Embedding fallback trap** — projects without explicit embedding settings fall
   back to hardcoded OpenAI-compatible defaults, not the seeded global Cohere values.
5. `docs/` is gitignored in this repo — these documents are local working copies
   unless you change `.gitignore`.

## 12. Verification commands (development)

```bash
make lint        # ruff check + format check
make typecheck   # mypy app/
make test        # pytest (~3 min; no live services needed — fakes only)
make check       # all three
```

Notes: `tests/test_jobs_advanced.py::test_retry_uses_jittered_backoff` is flaky
(wall-clock bounds) — rerun it alone before debugging. `app/scripts/benchmark.py`
is a heavy load test against the real engine (defaults 10 concurrent × 100 topics);
do not run casually.
