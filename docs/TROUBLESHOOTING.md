# EzDistro Platform — Troubleshooting Guide

Symptom-driven diagnostics. Every entry states how to **verify** the cause before
changing anything. Deeper background: FAILURES.md (failure-mode matrix),
ARCHITECTURE.md (how the engine works).

```text
Documentation status:  Verified against code & failure handling paths
                       (research engine)
Last verified:         2026-10-04
```

How to read a job's history first (applies everywhere below):
**Jobs** → job detail → event timeline. The timeline names the failing stage, the
provider, the error category, and whether the error was classified *retryable*
(auto-retries with backoff) or *permanent* (needs human action). Admins additionally
see structured `errorDetails` including the traceback tail for unexpected crashes.

---

## 1. Authentication & access

### Symptom: every page redirects to /login even after logging in

- **Verify:** `GET /health` → `503 {"status":"degraded","pocketbase":"unreachable"}`?
  Auth refreshes fail without PocketBase.
- **Resolution:** restore/verify PocketBase first (`PB_URL` correct? PB process up?).
  The web process itself is fine — it gates everything on auth.

### Symptom: "permission" toast on project actions; others can proceed

- **Verify:** your membership + role in that project. Platform admins bypass checks;
  members need `owner`/`admin`/`editor` to mutate, `owner`/`admin` for destructive ops.
  A user with **zero memberships sees no projects by design** (empty scope ≠ global).
- **Resolution:** ask an owner/admin to add you under **project_members** with the
  right role.

## 2. Integrations (**Connections**)

### Symptom: connection test fails with a private-address/network error

- **Cause:** the SSRF guard rejects private/loopback/link-local targets.
- **Verify:** target URL host is `localhost`, `127.x`, `10.x`, `192.168.x`, etc.
- **Resolution:** production should keep the guard on and use reachable hosts. For
  local development only, start processes with `ALLOW_PRIVATE_NETWORKS=1`.

### Symptom: WordPress test fails / publishing returns auth errors

- **Verify:** application passwords enabled for the WP account (WP Admin → Users →
  Profile → Application Passwords); username matches; URL includes the full site path
  and scheme.
- **Resolution:** fix credentials in **Connections**, re-run **Test**, then re-dispatch the
  failed publish job from **Failed jobs** (**Retry**). WordPress 401s are permanent
  errors — jobs do not auto-retry them.

## 3. Bootstrap / schema

### Symptom: `make bootstrap` exits with "collection import failed"

- **Verify:** the error text names reachability vs credentials. Requires
  `PB_ADMIN_EMAIL`/`PB_ADMIN_PASSWORD` of a superuser and **PocketBase ≥ 0.23**.
- **Resolution:** fix env/PB version and re-run — bootstrap is idempotent.

### Symptom: a collection exists but lacks newer fields

- **Cause:** bootstrap merges additively and never deletes fields/collections
  removed from code.
- **Verify:** compare the live collection against `app/scripts/bootstrap_pb.py`.
- **Resolution:** re-run `make bootstrap` after deploying current code; manually
  reconcile any intentionally-removed fields.

## 4. Worker not processing jobs

### Symptom: jobs stay `pending`

Checklist, in order:

1. Is a worker running at all? Its startup log lists worker id + concurrency caps.
   No worker ⇒ start one (`make worker`). It needs admin creds.
2. Worker logs showing `worker.poll.error`? That means PocketBase calls fail — fix PB
   connectivity; polling resumes automatically afterwards.
3. Job `availableAt` in the future? That's backoff/scheduling, not a bug — it will be
   claimed when due (poll every ~5 s by default).
4. Project archived? Scheduled work only fires for `active` projects.

### Symptom: job stuck `running` forever

- **Cause:** a dead worker held the lease.
- **Verify:** job detail shows `lockedBy`/`leaseExpiresAt`; if lease expiry passed,
  another poll cycle reclaims it automatically.
- **Resolution:** wait out `LEASE_SECONDS` (300 s default) with some worker alive; no
  manual reset required. Repeated immediate crashes → see the job's traceback tail
  (admins) and FAILURES.md crash matrix.

## 5. Retries & failures

### Symptom: job flips between `running`/`retrying` many times

- **Meaning:** transient provider errors (timeout / 429 / 5xx / network) are retrying
  with exponential backoff + ±50 % jitter; rate-limit headers raise the delay floor.
- **Verify:** timeline shows `job.retry_scheduled` entries with delays; error category
  per attempt.
- **Resolution:** usually none — it converges when the provider recovers. If it
  exhausts `max_attempts` (default 3), it lands in `/failed` for manual retry once the
  underlying issue is fixed.

### Symptom: job failed immediately and never retried

- **Meaning:** permanent classification — bad credentials, invalid request, schema
  validation, config errors, missing entities.
- **Verify:** `errorCode`/`errorMessage` + details on the job record.
- **Resolution:** fix the root cause (key, config, prompt variables…), then
  **Retry**. Manual retry resets attempts and makes the job immediately claimable.

## 6. Indexing

### Symptom: run fails fast with "qdrant dimension mismatch"

- **Cause:** embedding model/dimensions changed while an old collection exists; upserts
  would mix incompatible vectors, so this fails as permanent by design.
- **Verify:** project settings embedding model/dims vs documents' recorded values;
  Qdrant collection name embeds the model slug (`ezdistro-{slug}-{model}`).
- **Recovery:** set the intended model/dimensions and trigger **Full re-index** — new
  namespace, nothing mixed. Old collections can be dropped manually when unused.

### Symptom: index run interrupted (worker restart, cancel)

- **Behavior:** runs checkpoint counters + `lastSourceId` every 10 posts. Retrying the
  job resumes from the checkpoint; unchanged posts are skipped via content hash, so
  re-runs are cheap and idempotent (stable point ids prevent vector duplication).
- **Verify:** the run row shows progress fields mid-flight; a retried run continues,
  it does not restart from zero unless forced.

### Symptom: deleted WP posts still retrievable

- **Cause:** stale vectors are only pruned during **full** reindexes.
- **Resolution:** run **Full re-index** — sources absent from the sweep are deleted in
  batches. Incremental runs never delete by design.

## 7. Writing pipeline (**Topics** / **Articles**)

### Symptom: article stuck with sections `pending`/`generating`

- **Normal transient state:** section jobs run independently; the assembler polls
  (20 s retry-after) until all are done. Watch the workspace (~2.5 s refresh).
- **If permanently stuck:** inspect each section job in **Jobs**. Failed sections show a
  human-readable error in the workspace plus technical detail for admins. Retry the
  failed section job; the assembler will complete afterwards.

### Symptom: section failed with "section failed validation"

- **Cause:** the LLM produced forbidden content (script/style tags, inline handlers,
  `javascript:` URLs, markdown fences, H1, unbalanced HTML, too-short output). This is
  a deliberate gate — garbage is never stored silently.
- **Resolution:** regenerate the section (or the article). If a specific prompt keeps
  producing violations, tighten the section prompts in **Prompts**.

### Symptom: write job failed with "prompt rendering failed (unknown variables…)"

- **Cause:** a prompt contains `{{ variable }}` names outside the registry, or an
  unclosed `{{`. Caught before any LLM call.
- **Resolution:** edit the prompt in **Prompts** using only listed variables; save
  creates a new version; reactivate the previous version to roll back instantly.

### Symptom: topic stuck in `planning`

- **Behavior:** this indicates a crashed earlier attempt. Re-running writing detects
  it, resets to `planned` and resumes (saved outline reused when valid).

## 8. Publishing

### Symptom: publish refused: "article is not approved for publishing"

- **By design.** Publishing requires status `approved` (or documented exceptions:
  updates to already-published articles, retries of never-published failures,
  same-job crash recovery).
- **Resolution:** review screen → **Approve** → **Publish** again.

### Symptom: duplicate-looking posts on WordPress?

- **Guards exist at three layers:** stored `wordpressPostId` forces update-not-create;
  before creating, orphan lookup by slug finds a post created during a crashed attempt
  and updates it; created posts carry `ezdistro_article_id` meta. Exactly one create ever.
- **Verify:** publishing tab shows one `published` run per successful attempt with the
  final post id/URL.

### Symptom: unpublish fails / behaves unexpectedly

> **Known conflict (documented honestly):** the unpublish flow writes
> `mode="unpublish"` and `status="unpublished"` into `publishing_runs`, whose select
> enums only allow `draft|publish` and `pending|published|failed|skipped_duplicate`.
> Against a bootstrapped database these writes violate validation and the action
> should be expected to fail. Verify on your deployment before relying on unpublish;
> the fix requires either a schema migration adding those enum values or a code
> change. As a workaround, set the post to private directly in WordPress; the article
> stays intact in EzDistro.

### Symptom: published article shows duplicated title heading

Fixed behavior: the leading `<h1>` is stripped before sending (WP themes render their
own). If you see duplicates, check whether the theme also injects the title inside the
content area — that's a theme setting, not a platform bug.

### Symptom: publish refused because the cover image is missing

- **By design.** `publish_article` blocks when no ready cover exists, unless the job
  payload sets `publishWithoutCover=true`.
- **Verify:** workspace images pane — is there a `ready` + active cover version?
- **Resolution:** generate/activate the cover (**Generate image** / **Use this image**),
  then publish again. If the image provider is down, publishing text-only requires
  the explicit override path.

## 9. Images (v1.3.0)

### Symptom: plan job fails with "article has no content yet"

- **Cause:** `plan_article_images` requires `finalHtml`/`generatedContent`.
- **Resolution:** complete the write pipeline (assemble) first, then plan.

### Symptom: "no active image integration — skipping automatic generation"

- **Cause:** the plan was stored, but no *enabled* `image`-category integration
  exists, so generation jobs were never queued.
- **Resolution:** **Connections** → add/enable an image integration (`gemini`/`bfl`/
  `openai_compat`) → re-plan (**Image plan**) or generate per slot manually.

### Symptom: generation fails fast on provider/integration mismatch

- Handlers fail fast (permanent) when the slot's configured provider has no
  matching active integration — fix the **Images** tab mapping instead of retrying.
  An HTML error page from the provider endpoint is classified distinctly from a
  real generation failure; check the job's error details.

### Symptom: image stuck `generating`/`optimizing`

- Like all jobs, image jobs checkpoint via leases: a crashed worker's job is
  reclaimed after lease expiry and retried per `imageMaxRetries`
  (`max(2, value)` attempts). Inspect the job event timeline for the provider
  error category (transient → auto-retry; permanent → fix config, then retry).

### Symptom: WordPress shows the pre-re-optimization image

- **Known behavior (documented in code):** a re-optimized image keeps its old
  `wordpressMediaId` — WordPress serves the previous file until the next publish
  re-attaches. Re-publish the article to refresh media.

## 10. Data stores

### Symptom: "credential cannot be decrypted — SECRETS_KEY changed?"

- **Cause:** integrations were encrypted with a different `SECRETS_KEY` (rotation or a
  different environment).
- **Resolution:** restore the original key, or re-enter all integration secrets under
  the new key. There is no backdoor by design.

### Symptom: retrieval returns nothing / writer ignores knowledge base

Checklist:

1. Indexing actually ran? **Indexing** tab shows indexed counts > 0.
2. Project has top-k > 0 and (optionally) threshold not filtering everything
   (runtime default threshold is 0 = disabled).
3. Use the **Retrieval** diagnostics tab with the topic's query — it shows raw hits,
   rerank scores, surviving link candidates and the exact prompt context.
4. Reranker enabled but its integration missing/unhealthy? Retrieval falls back to
   vector order (never blocks writing).

## 11. UI oddities

### Toast says "Project not found or no access"

Either the id doesn't exist or it belongs to a project outside your scope
(cross-project ids are deliberately indistinguishable from missing ones).

### Page didn't change after clicking a button

Mutations require HTMX; responses drive toasts/events. If JavaScript was blocked the
action may not apply — retry with scripts enabled. All mutations answer with visible
toasts; a silent click means the request never reached the server (network)
or was rejected pre-handler.

## 12. SEO research (Google Ads / SERP / WordPress sync)

A dedicated symptom table lives in [SEO_RESEARCH.md](SEO_RESEARCH.md#troubleshooting).
The most common ones:

### Symptom: "Google Ads is not configured for this project"

- **Cause:** no enabled `google_ads` connection on the project, and env `GOOGLE_ADS_*`
  is empty (env is only a fallback).
- **Resolution:** **Connections** → add/edit the Google Ads connection (client id,
  client secret, redirect URI) → connect the account on the **Research** tab.

### Symptom: `redirect_uri_mismatch`

- The redirect URI in the connection must match the Google Cloud registration exactly
  (scheme, host, path). Both `/auth/google-ads/callback` and
  `/projects/google-ads/callback` are served and interchangeable.

### Symptom: research run stuck `running`

- The worker must be running (`make worker`); runs are resumable and continue from the
  last completed stage on restart. Check the run's job in **Jobs** for the failing stage.

### Symptom: mirrored WordPress posts missing / `update_available`

- Sync is paged and incremental; **Refresh accounts**/re-run the research. A remote
  change over local work is flagged `update_available` by design — the engine refuses
  to overwrite local edits.

### Symptom: SERP tab shows "not configured"

- Expected without a `serp` integration. Add a `serper` connection to enable SERP data;
  ranking/PAA/snippet fields are otherwise shown *unavailable*, never estimated.

---

## Escalation pointers

- Failure semantics per dependency: FAILURES.md
- Engine internals (leases, backoff math, idempotency): ARCHITECTURE.md §5
- Schema/enum reference: SCHEMA.md §3.15 (publishing_runs caveat lives here too)
