"""Indexing pipelines ("index_project" and "index_document").

Production indexing engine:

1. load project settings (chunking, embedding, vector)
2. resolve the WordPress publisher from the project's integrations
3. fetch WordPress posts incrementally (id-ordered, paginated, resumable)
4. normalize content (strip HTML → plain text)
5. extract title / rendered HTML / plain text / source URL / post ID / metadata
6. content-hash the plain text
7. compare against stored document metadata
8. skip unchanged documents — WITHOUT regenerating embeddings
9. metadata-only changes → update metadata (+ vector payload) WITHOUT re-embedding
10. re-index changed/new documents: chunk → batch-embed → upsert Qdrant →
    store metadata → mark indexed

Freshness is NEVER decided by post-id existence — only by content hash (plus a
chunk-count guard: if the chunking configuration changed, the same hash no
longer maps to the same vectors, so the document is re-indexed).

Run counters: discovered / unchanged / changed / indexed / skipped / failed /
elapsed — persisted on the index_runs record and returned in the result.

Stale cleanup: a FULL run (payload.full=true) deletes vectors whose source_id
was not seen during the run (document replacement).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from app.domain.chunker import chunk_text, strip_html
from app.jobs.context import JobCancelled, JobContext
from app.jobs.handlers import register_job
from app.providers.base import PermanentError, VectorPoint, WPPost
from app.providers.vector.qdrant_store import payload_safe, point_id
from app.repositories.indexing import DocumentRepo, IndexRunRepo

CHECKPOINT_EVERY = 10  # documents processed before persisting progress/counters
SOURCE_TYPE = "wordpress"


@register_job("index_project")
async def handle_index_project(ctx: JobContext) -> dict[str, Any]:
    config = ctx.config
    project_id = ctx.project_id
    run_repo = IndexRunRepo(ctx.pb)
    doc_repo = DocumentRepo(ctx.pb)
    payload = ctx.payload()
    force = bool(payload.get("force"))
    full = bool(payload.get("full"))

    ctx.info(
        "index run started",
        {"trigger": payload.get("trigger", "manual"), "force": force, "full": full},
    )
    ctx.stage_started("fetch_posts", "در حال دریافت نوشته‌ها از وردپرس")
    ctx.progress(2, stage="fetch_posts", message="در حال آماده‌سازی…")

    publisher = ctx.registry.get_publisher_provider(config.project, config.settings)
    vector = ctx.providers.vector

    dimensions = int(config.embedding["dimensions"])
    try:
        await vector.ensure_collection(dimensions)
    except ValueError as exc:
        raise PermanentError(f"qdrant dimension mismatch: {exc}") from exc
    ctx.info("vector collection ready", {"dimensions": dimensions})

    chunk_cfg = _chunk_config(config)
    # Resume: reuse the run record from a previous (failed/crashed) attempt of
    # THIS job; a retried run may also carry an explicit checkpoint (resumeFrom).
    run = run_repo.first(filter=f'job="{ctx.job_id}"')
    if run is None or run.get("status") in ("succeeded", "cancelled"):
        run = run_repo.start(
            project=project_id,
            job=ctx.job_id,
            trigger=payload.get("trigger", "manual"),
            last_source_id=str(payload.get("resumeFrom") or ""),
        )
    else:
        run = run_repo.update(run["id"], {"status": "running", "error": {}})
    run_id = run["id"]
    after_id = int(run.get("lastSourceId") or 0)

    totals = {
        "discovered": 0,
        "unchanged": 0,
        "changed": 0,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
    }
    seen_sources: set[str] = set()

    try:
        per_page = 100
        processed_since_checkpoint = 0
        while True:
            await ctx.check_cancelled()
            posts = await publisher.list_posts(
                per_page=per_page, after_id=after_id, status="publish"
            )
            if not posts:
                break
            totals["discovered"] += len(posts)
            ctx.progress(
                5, stage="fetch_posts", message=f"در حال بررسی {totals['discovered']} نوشته…"
            )

            for post in posts:
                await ctx.check_cancelled()
                await _process_post(
                    ctx,
                    post=post,
                    run_id=run_id,
                    chunk_cfg=chunk_cfg,
                    dimensions=dimensions,
                    force=force,
                    doc_repo=doc_repo,
                    totals=totals,
                    seen_sources=seen_sources,
                )
                after_id = max(after_id, int(post.id))
                processed_since_checkpoint += 1

                if processed_since_checkpoint >= CHECKPOINT_EVERY:
                    _checkpoint(run_repo, run_id, totals, after_id)
                    pct = 10 + min(
                        85, int(85 * totals["discovered"] / max(1, totals["discovered"]))
                    )
                    ctx.progress(
                        pct, stage="embedding", message=f"{totals['discovered']} نوشته بررسی شد"
                    )
                    processed_since_checkpoint = 0

            if len(posts) < per_page:
                break

        # Stale vector cleanup — ONLY on explicit full runs (document replacement).
        stale_deleted = 0
        if full:
            stale_deleted = await _cleanup_stale_vectors(ctx, seen_sources)

        _checkpoint(run_repo, run_id, totals, after_id)
        run_repo.finish(run_id, "succeeded")
        ctx.stage_completed("embedding", "پایان نمایه‌سازی")
        ctx.progress(100, stage="done", message="پایان نمایه‌سازی")
        result = {"runId": run_id, **totals, "staleDeleted": stale_deleted}
        ctx.info("index run finished", result)
        return result

    except JobCancelled:
        run_repo.finish(run_id, "cancelled")
        raise
    except Exception as exc:
        run_repo.finish(run_id, "failed", {"type": type(exc).__name__, "message": str(exc)})
        raise


@register_job("index_document")
async def handle_index_document(ctx: JobContext) -> dict[str, Any]:
    """Index a single source document (payload: sourceId, force?) — idempotent."""
    source_id = str(ctx.payload().get("sourceId") or "").strip()
    if not source_id:
        raise ValueError("index_document payload is missing sourceId")
    force = bool(ctx.payload().get("force"))

    config = ctx.config
    project_id = ctx.project_id
    ctx.info("index document started", {"source_id": source_id, "force": force})
    ctx.progress(5, stage="fetch", message="در حال دریافت نوشته…")

    publisher = ctx.registry.get_publisher_provider(config.project, config.settings)
    post = await publisher.get_post(int(source_id))
    if post is None:
        raise ValueError(f"wordpress post {source_id} not found")

    dimensions = int(config.embedding["dimensions"])
    try:
        await ctx.providers.vector.ensure_collection(dimensions)
    except ValueError as exc:
        # Wrong embedding dimension for the existing collection: retrying will
        # never help — the model/namespace must be fixed by a human.
        raise PermanentError(f"qdrant dimension mismatch: {exc}") from exc
    run_repo = IndexRunRepo(ctx.pb)
    run = run_repo.start(
        project=project_id, job=ctx.job_id, trigger="manual", last_source_id=source_id
    )
    totals = {
        "discovered": 1,
        "unchanged": 0,
        "changed": 0,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
    }
    seen: set[str] = set()
    try:
        await _process_post(
            ctx,
            post=post,
            run_id=run["id"],
            chunk_cfg=_chunk_config(config),
            dimensions=dimensions,
            force=force,
            doc_repo=DocumentRepo(ctx.pb),
            totals=totals,
            seen_sources=seen,
        )
        run_repo.finish(run["id"], "succeeded")
        ctx.progress(100, stage="done", message="نمایه‌سازی انجام شد")
        return {"runId": run["id"], "sourceId": source_id, **totals}
    except Exception as exc:
        run_repo.finish(run["id"], "failed", {"type": type(exc).__name__, "message": str(exc)})
        raise


# ---------------------------------------------------------------------------
# Per-document pipeline
# ---------------------------------------------------------------------------
async def _process_post(
    ctx: JobContext,
    *,
    post: WPPost,
    run_id: str,
    chunk_cfg: dict[str, Any],
    dimensions: int,
    force: bool,
    doc_repo: DocumentRepo,
    totals: dict[str, int],
    seen_sources: set[str],
) -> None:
    """Process one document. Never duplicates irreversible work.

    Freshness is decided by content hash (+ chunk-config guard), never by the
    mere existence of the post id.
    """
    project_id = ctx.project_id
    config = ctx.config
    wp_id = int(post.id)
    source_id = str(wp_id)
    seen_sources.add(source_id)

    # 4-5. normalize + extract
    plain_text = strip_html(post.content_html)
    content_hash = hashlib.sha256(plain_text.encode("utf-8")).hexdigest()
    existing = doc_repo.by_source(project_id, SOURCE_TYPE, source_id)

    # 7-8. freshness by content hash (+ chunk-config guard), never by post-id
    # existence. A record left `pending` by a crashed run is ALWAYS re-indexed.
    # The vector namespace is keyed by (project, embedding model) — if the
    # embedding provider/model/dimensions changed since the doc was indexed, the
    # doc must be re-embedded into the new namespace (see HIGH H2).
    if (
        not force
        and existing
        and existing.get("contentHash") == content_hash
        and existing.get("indexStatus") == "indexed"
        and str(existing.get("embeddingModel") or "") == str(config.embedding["model"])
        and int(existing.get("embeddingDimensions") or 0) == int(dimensions)
    ):
        expected_chunks = len(chunk_text(plain_text, **chunk_cfg))
        if int(existing.get("chunkCount") or 0) == expected_chunks:
            # 8. unchanged content → do NOT regenerate embeddings.
            if _metadata_changed(existing, post):
                # 9. metadata-only change: update record + vector payloads.
                await _update_metadata_only(
                    ctx, existing, post, content_hash, expected_chunks, run_id, doc_repo
                )
                totals["changed"] += 1
            else:
                totals["unchanged"] += 1
            return

    chunks = chunk_text(plain_text, **chunk_cfg)
    if not chunks:
        totals["skipped"] += 1
        return

    # 13. metadata FIRST (need the document_id for vector payloads)
    record = doc_repo.upsert_metadata(
        project=project_id,
        source_type=SOURCE_TYPE,
        source_id=source_id,
        title=post.title,
        source_url=post.link,
        content_hash=content_hash,
        embedding_provider=config.embedding["provider"],
        embedding_model=config.embedding["model"],
        embedding_dimensions=dimensions,
        chunk_count=len(chunks),
        run_id=run_id,
    )

    namespace_key = _namespace(ctx)
    try:
        # 11. batch embeddings
        started = time.monotonic()
        vectors = await ctx.providers.embedding.embed_documents(chunks)
        latency_ms = int((time.monotonic() - started) * 1000)
        # 12. upsert into Qdrant
        points = [
            VectorPoint(
                id=point_id(config.slug, wp_id, i),
                vector=vectors[i],
                payload=payload_safe(
                    {
                        "project": namespace_key,
                        "project_id": project_id,
                        "document_id": record["id"],
                        "source_id": source_id,
                        "source_url": post.link,
                        "title": post.title,
                        "chunk_index": i,
                        "content_hash": content_hash,
                        "language": config.language,
                        "created_at": _now_iso(),
                        "chunk_text": chunks[i][:4000],
                    }
                ),
            )
            for i in range(len(chunks))
        ]
        await ctx.providers.vector.upsert(points)
        ctx.info(
            "post indexed",
            {"source_id": source_id, "chunks": len(chunks), "latency_ms": latency_ms},
        )
    except Exception as exc:
        totals["failed"] += 1
        ctx.warning("post indexing failed", {"source_id": source_id, "error": str(exc)})
        doc_repo.set_status(record["id"], "failed")
        return

    # 14. mark indexed (record was created as `pending` — crash-safe)
    doc_repo.set_status(record["id"], "indexed")
    if existing:
        totals["changed"] += 1
    totals["indexed"] += 1


async def _update_metadata_only(
    ctx: JobContext,
    existing: dict[str, Any],
    post: WPPost,
    content_hash: str,
    chunk_count: int,
    run_id: str,
    doc_repo: DocumentRepo,
) -> None:
    """Metadata changed but content did not → update WITHOUT re-embedding."""
    doc_repo.update(
        existing["id"],
        {
            "title": post.title,
            "sourceUrl": post.link,
            "contentHash": content_hash,
            "chunkCount": chunk_count,
            "lastRun": run_id,
            "indexStatus": "indexed",
        },
    )
    # keep vector payloads in sync (title/source_url) without touching vectors
    point_ids = [point_id(ctx.config.slug, int(post.id), i) for i in range(chunk_count)]
    try:
        await ctx.providers.vector.update_payload(
            point_ids,
            {"title": post.title, "source_url": post.link, "content_hash": content_hash},
        )
    except Exception as exc:
        ctx.warning(
            "vector payload update failed (metadata kept in PocketBase)",
            {"source_id": post.id, "error": str(exc)},
        )
    ctx.info("document metadata updated without re-embedding", {"source_id": post.id})


async def _cleanup_stale_vectors(ctx: JobContext, seen_sources: set[str]) -> int:
    """Delete vectors whose source_id was not seen in this full run."""
    project_id = ctx.project_id
    ctx.stage_started("cleanup", "پاک‌سازی برداری‌های قدیمی")
    try:
        all_points = await ctx.providers.vector.scroll_ids({"project_id": project_id})
        stale = [pid for pid, pl in all_points if str(pl.get("source_id")) not in seen_sources]
        for i in range(0, len(stale), 100):
            await ctx.providers.vector.delete(stale[i : i + 100])
        if stale:
            ctx.info("stale vectors cleaned", {"deleted": len(stale)})
        return len(stale)
    except Exception as exc:
        ctx.warning("stale cleanup failed", {"error": str(exc)})
        return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _metadata_changed(existing: dict[str, Any], post: WPPost) -> bool:
    return str(existing.get("title") or "") != str(post.title) or str(
        existing.get("sourceUrl") or ""
    ) != str(post.link)


def _chunk_config(config: Any) -> dict[str, Any]:
    cfg = config.chunking
    return {
        "size": max(50, int(cfg.get("size") or 500)),
        "overlap": max(
            0, min(int(cfg.get("overlap") or 100), max(50, int(cfg.get("size") or 500)) - 1)
        ),
        "strategy": str(cfg.get("strategy") or "auto"),
        "max_chunks": max(0, int(cfg.get("max_chunks") or 0)),
    }


def _checkpoint(
    run_repo: IndexRunRepo, run_id: str, totals: dict[str, int], last_source_id: int
) -> None:
    run_repo.update_progress(
        run_id,
        processedDocuments=totals["discovered"],
        unchangedDocuments=totals["unchanged"],
        changedDocuments=totals["changed"],
        indexedDocuments=totals["indexed"],
        skippedDocuments=totals["skipped"],
        failedDocuments=totals["failed"],
        totalDocuments=totals["discovered"],
        lastSourceId=str(last_source_id),
    )


def _now_iso() -> str:
    from app.repositories.jobs import now_utc, pb_dt

    return pb_dt(now_utc())


def _namespace(ctx: JobContext) -> str:
    from app.providers.registry import qdrant_namespace

    return qdrant_namespace(ctx.config.project, ctx.config.settings)
