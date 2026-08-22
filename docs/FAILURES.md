# FAILURES.md — Failure Engineering Matrix

Every external dependency can fail. This document defines, for each failure mode,
the exact behavior: **retry? how many? backoff? user-visible message? permanent?
manual recovery?**

Two retry layers exist:

1. **Adapter layer** (`with_retry` in `app/providers/http.py`) — retries
   network-level errors *inside a single call* (timeouts, connect errors, transient
   HTTP statuses). Default `attempts=3`, exponential backoff from a 2 s base delay;
   the provider's `Retry-After` header is captured into error details so the job
   layer can honor it.
2. **Job layer** (engine `_finalize_failure`) — a transient failure marks the job
   `retrying` with exponential backoff + jitter (±50 %), honoring the provider's
   `Retry-After` when present (`delay = max(delay, retry_after_seconds)`).
   `max_attempts` comes from the project `retryPolicy` (default 3); backoff base
   30 s, cap 3600 s. After the last attempt → `failed` (permanent).

Transient ⇒ retryable. Permanent ⇒ job fails; the UI shows the error and the job can
be re-dispatched manually (**تلاش مجدد**).

```text
Documentation status:  Refreshed & verified against current code
Last verified:         2026-08-22
```

## Cohere / embeddings

| Failure | Retry? | Attempts | Backoff | User-visible | Permanent? | Manual recovery |
|---|---|---|---|---|---|---|
| timeout (read/connect) | adapter + job | 3 + 3 | exp 2 s; jittered job backoff | «محدودیت زمانی تأمینکننده» on job failure | no | re-dispatch the job |
| 429 rate limit | adapter + job | 3 + 3 | honors `Retry-After` | «محدودیت نرخ تأمینکننده» | no | none (auto) |
| 500 / 5xx | adapter + job | 3 + 3 | exp | «خطای موقت تأمینکننده» | no | none (auto) |
| malformed response (wrong shape / fewer embeddings than requested) | **no** | — | — | «پاسخ نامعتبر از تأمینکننده» | **yes** | check provider/model config; re-dispatch |

## LLM (OpenAI-compatible / Gemini)

| Failure | Retry? | Attempts | Backoff | User-visible | Permanent? | Manual recovery |
|---|---|---|---|---|---|---|
| timeout | adapter + job | 3 + 3 | exp 2 s; jittered | «محدودیت زمانی مدل زبانی» | no | none (auto) |
| 429 | adapter + job | 3 + 3 | honors `Retry-After` | «محدودیت نرخ مدل زبانی» | no | none (auto) |
| invalid JSON output | **job only** (ValueError ⇒ transient) | 3 | jittered | «خروجی مدل قابل تحلیل نبود» | no | none (auto) |
| truncated response (missing content) | **no** | — | — | «پاسخ مدل ناقص بود» | **yes** | re-dispatch |
| empty response | **no** | — | — | «پاسخ مدل خالی بود» | **yes** | re-dispatch |

Note: outline JSON additionally gets **one deterministic repair pass** through the
`validation` prompt before the error is raised.

## Qdrant (vector store)

| Failure | Retry? | Attempts | Backoff | User-visible | Permanent? | Manual recovery |
|---|---|---|---|---|---|---|
| timeout | job layer (raw `TimeoutError` ⇒ transient) | 3 | jittered | «محدودیت زمانی کیوذرنت» | no | none (auto) |
| connection failure | job layer (raw `ConnectionError` ⇒ transient) | 3 | jittered | «اتصال به کیوذرنت برقرار نشد» | no | check Qdrant is up; auto |
| dimension mismatch (`ensure_collection` ValueError) | **no** — classified `PermanentError` by the indexing handlers | — | — | «ابعاد برداری با مدل جاسازی ناسازگار است» | **yes** | fix embedding model/dimensions; full reindex into the new namespace |

Per-post indexing failures never abort the run: they increment `failedDocuments`,
mark that document `failed`, and the run continues.

## WordPress

| Failure | Retry? | Attempts | Backoff | User-visible | Permanent? | Manual recovery |
|---|---|---|---|---|---|---|
| timeout | adapter + job | 3 + 3 | exp 2 s; jittered | «محدودیت زمانی وردپرس» | no | none (auto) |
| 401 auth error | **no** | — | — | «احراز هویت وردپرس ناموفق بود» | **yes** | fix application password in Integrations; re-dispatch |
| invalid content (400) | **no** | — | — | «محتوا توسط وردپرس رد شد» | **yes** | fix content/template; re-dispatch |
| duplicate post (crash between `create_post` and storing the id) | job (safe retry) | up to max_attempts | jittered | recovery is silent — «مقاله منتشر شد» | no | none — the retry looks up the orphaned post **by slug** (`find_post_by_slug`, WP REST slug filter) and UPDATEs it; created posts also carry `seoz_article_id` meta |

Workflow-gate refusals ("article is not approved for publishing…") are permanent,
non-retried errors — approval state is enforced in the handler, not just the UI.

> ⚠️ **Unpublish caveat:** `_unpublish` writes `mode="unpublish"` /
> `status="unpublished"` values that the bootstrapped `publishing_runs` select enums
> do not include (`mode`: draft\|publish; `status`: pending\|published\|failed\|
> skipped_duplicate). Expect schema-validation failure until either the schema or the
> code is fixed. See SCHEMA.md §3.14 and TROUBLESHOOTING.md §8.

## PocketBase

| Failure | Retry? | Attempts | Backoff | User-visible | Permanent? | Manual recovery |
|---|---|---|---|---|---|---|
| temporary unavailability during a job (incl. config load) | job layer (network errors ⇒ transient) | 3 | jittered | «خطای موقت پایگاه داده» | no | none (auto) |
| temporary unavailability during polling | worker loop catches; polls again next cycle | ∞ (until recovery) | poll interval | none | no | none (auto) |
| stale job (worker died mid-flight; lease expired) | reclaimed by any worker once `leaseExpiresAt` passes | 1 (re-execution) | n/a | none | no | none (auto) |

## Worker crash simulations (kill + restart)

Verified by tests in `tests/test_failure_engineering.py`:

| Crash point | Duplicated sections? | Duplicated WP posts? | Corrupt article state? | Recovery |
|---|---|---|---|---|
| during indexing | n/a | n/a | run record resumes from checkpoint; **no duplicate vector points** (deterministic point ids `{slug}:{wp_id}:{chunk}`) | restarted worker reclaims the stale run and finishes it |
| during section generation | **no** — `(article, position)` unique forces UPDATE of the same row | n/a | article stays `generating`; assemble keeps waiting (20 s retry-after) | sections regenerate into the same rows; assemble completes → `review` |
| between WP `create_post` and storing the post id | n/a | **no** — retry finds the orphan via slug lookup and UPDATEs it (exactly one CREATE ever) | article stuck in `publishing`; the same job's retry is allowed (guard checks the job's own run record) | publish completes; article → `published` |
| during outline generation | n/a | n/a | topic stuck in `planning` | retried `write_article` detects the crash artifact, resets to `planned` and resumes; saved outline reused when present |
| full batch (write_article × 3) | **no** — exactly 9 sections, unique (article, position) | n/a | all articles end `review` with `finalHtml` | every job completes; exactly 3 assembles |

Guarantees:

- Job execution is **at-least-once**; side effects are made idempotent (deterministic
  ids, unique constraints, slug/meta lookups, update-instead-of-create).
- The only unavoidable duplicate window (WP `create_post` + immediate crash) is closed
  by the slug-based orphan recovery on retry.
- A job is never lost: `pending`/`retrying`/`running` states are all claimable
  (running only after lease expiry).

## Operational notes

- Retry policy is per-project (`retryPolicy`): `max_attempts` (3),
  `backoff_base` (30 s), `backoff_max` (3600 s). Formula:
  `delay = min(base × 2^(attempts−1), cap) × uniform(0.5–1.5)`.
- `Retry-After` from 429 responses is always honored as a delay floor.
- Permanent failures keep the full error (`errorCode`, `errorMessage`,
  `errorDetails`) — technical detail renders for admins only; manual retry creates a
  fresh attempt under the same idempotency key.
- No retry storms: bounded concurrency (job / LLM / embedding / publish semaphores)
  plus adapter-level attempt caps keep cascading failures contained.

### Known gaps (documented, not yet fixed)

1. Unpublish flow vs `publishing_runs` enums — see the WordPress table above.
2. `PublishingRunRepo.start` writes a `requestId` field that is not part of the
   collection schema; PocketBase drops it at rest. Request ids remain available via
   `responseMetadata.requestId` written on completion/failure paths that set metadata.
