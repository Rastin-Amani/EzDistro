"""RetrievalService — the single retrieval path for SEO content generation.

1. normalize query
2. embed the query (EmbeddingProvider.embed_queries)
3. search the vector store (candidate pool)
4. apply the project_id filter (ALWAYS — namespace is defense-in-depth)
5. apply the configurable candidate count + minimum similarity threshold
6. optionally rerank a larger pool down to top N (never mandatory)
7. return normalized RetrievalResult objects

Domain code (writer, diagnostics) consumes only RetrievalResult — swapping the
embedding/vector/rerank providers never touches it.
"""

from __future__ import annotations

import re
from typing import Any

from app.providers.base import SearchHit
from app.providers.registry import ProviderRegistry
from app.schemas.retrieval import RetrievalOptions, RetrievalResult
from app.services.settings import ProjectConfig

SNIPPET_CHARS = 500


def normalize_query(query: str) -> str:
    """Trim, strip HTML, collapse whitespace — queries come from titles/keywords."""
    if not query:
        return ""
    text = re.sub(r"<[^>]+>", " ", query)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class RetrievalService:
    def __init__(self, pb: Any, registry: ProviderRegistry | None = None) -> None:
        self._pb = pb
        self._registry = registry or ProviderRegistry(pb)

    # ---------------------------------------------------------------------------
    def options_from_config(self, config: ProjectConfig) -> RetrievalOptions:
        r = config.retrieval
        return RetrievalOptions(
            candidate_count=int(r.get("top_k") or 20),
            similarity_threshold=float(r.get("similarity_threshold") or 0.0) or None,
            rerank=bool(r.get("rerank_enabled")),
            rerank_top_n=int(r.get("rerank_top_n") or 8),
        )

    # ---------------------------------------------------------------------------
    async def retrieve(
        self,
        config: ProjectConfig,
        query: str,
        options: RetrievalOptions | None = None,
    ) -> list[RetrievalResult]:
        """Full retrieval pipeline. Never raises for provider hiccups in the
        vector/rerank stage beyond ProviderError classification."""
        query = normalize_query(query)
        if not query:
            return []
        options = options or self.options_from_config(config)

        # 2. query embedding
        embedding = self._registry.get_embedding_provider(config.project, config.settings)
        vector = (await embedding.embed_queries([query]))[0]

        # 3-5. vector search: project_id filter ALWAYS applied; threshold optional
        store = self._registry.get_vector_provider(config.project, config.settings)
        hits = await store.query(
            vector,
            top_k=options.candidate_count,
            threshold=options.similarity_threshold,
            filters={"project_id": config.id},
        )

        results = [_result_from_hit(hit) for hit in hits]
        # Deduplicate by source URL (chunks from the same document) — keep the
        # highest-scoring chunk. Fewer duplicates → smaller prompt context.
        results = _dedupe_by_source(results)
        if not results:
            return []

        # 6. optional reranking — never mandatory; falls back to vector order
        if options.rerank:
            reranked = await self._rerank(config, query, results, options.rerank_top_n)
            if reranked is not None:
                return _dedupe_by_source(reranked)
        return results

    async def _rerank(
        self,
        config: ProjectConfig,
        query: str,
        results: list[RetrievalResult],
        top_n: int,
    ) -> list[RetrievalResult] | None:
        reranker = self._registry.get_reranker_provider(config.project, config.settings)
        if reranker is None:
            return None  # no reranker configured → vector order stands
        ranked = await reranker.rerank(
            query=query,
            documents=[r.snippet for r in results],
            top_n=min(top_n, len(results)),
            metadata=[r.metadata for r in results],
        )
        if not ranked:
            return None
        out: list[RetrievalResult] = []
        for item in ranked:
            if 0 <= item.index < len(results):
                base = results[item.index]
                out.append(
                    RetrievalResult(
                        document_id=base.document_id,
                        title=base.title,
                        source_url=base.source_url,
                        snippet=base.snippet,
                        vector_score=base.vector_score,
                        rerank_score=item.score,
                        final_score=item.score,
                        metadata=base.metadata,
                    )
                )
        return out


def _result_from_hit(hit: SearchHit) -> RetrievalResult:
    payload = hit.payload or {}
    return RetrievalResult(
        document_id=str(payload.get("document_id") or hit.id),
        title=str(payload.get("title") or ""),
        source_url=str(payload.get("source_url") or ""),
        snippet=str(payload.get("chunk_text") or "")[:SNIPPET_CHARS],
        vector_score=float(hit.score),
        metadata={
            k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, list, dict))
        },
    )


def _dedupe_by_source(results: list[RetrievalResult]) -> list[RetrievalResult]:
    """Keep the highest-scoring result per source_url.

    Chunks WITHOUT a source URL are always kept (they carry the passages).
    """
    best: dict[str, RetrievalResult] = {}
    order: list[str] = []
    unlinked: list[RetrievalResult] = []
    for result in results:
        url = (result.source_url or "").rstrip("/")
        if not url:
            unlinked.append(result)
            continue
        if url not in best:
            best[url] = result
            order.append(url)
        elif result.final_score > best[url].final_score:
            best[url] = result
    kept = unlinked + [best[url] for url in order]
    kept.sort(key=lambda r: r.final_score, reverse=True)
    return kept
