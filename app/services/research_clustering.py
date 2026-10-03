"""Semantic clustering for keyword research.

Three strategies, cheapest first — all optional, all replaceable:

- ``deterministic`` lexical similarity (token + phrase overlap) gated by an
  intent-modifier signature. Zero API calls, repeatable, O(n·k).
- ``embedding`` reuses the project's configured embedding provider — the same
  one indexing already uses, so there is no second embedding pipeline — and
  greedily assigns each keyword to the nearest cluster representative.
- ``llm`` batched regrouping with the ``cluster_system``/``cluster_user``
  prompts. Output is validated: a model may only regroup the ids we sent.

Clustering groups keywords that were already collected from Google Ads. It never
invents search volumes, competition or CPC, and lexical/embedding similarity on
its own never decides that two queries are the same *intent* — the
intent-modifier signature must agree too.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.article_html import slugify
from app.domain.keywords import (
    INTENT_MODIFIERS,
    content_tokens,
    head_term,
    similarity,
    tokens,
)
from app.domain.parsing import extract_json
from app.providers.base import GenerationParams, ProviderError
from app.repositories.research import ClusterRepo, KeywordMetricRepo, KeywordRepo
from app.services.prompt_service import PromptService

SIMILARITY_THRESHOLD = 0.62
EMBEDDING_THRESHOLD = 0.72
CANDIDATE_CAP = 40
EMBED_BATCH = 256
LLM_BATCH = 120
LLM_LABEL_MAX = 120
LOAD_PAGE = 500
MAX_KEYWORDS = 100_000
METHODS = ("auto", "deterministic", "embedding", "llm")
DETERMINISTIC_SOURCE = "deterministic_v1"
LLM_SOURCE = "llm_cluster_v1"
INTENTS = frozenset(
    {
        "informational",
        "commercial",
        "transactional",
        "navigational",
        "local",
        "comparison",
        "unknown",
    }
)

_COMPARISON = frozenset({"vs", "versus", "compare", "comparison", "alternatives", "vs."})
_COMMERCIAL = frozenset(
    {
        "best",
        "top",
        "review",
        "reviews",
        "price",
        "pricing",
        "cost",
        "cheap",
        "free",
        "buy",
        "demo",
        "trial",
    }
)
# Words that name the *topic* rather than the intent, plus superlatives that do
# not change the underlying need: "gym software" and "best gym software" are the
# same opportunity, "gym software pricing" is not.
_TOPIC_WORDS = frozenset({"software", "tool", "tools", "app", "apps"})
_NEUTRAL_MODIFIERS = frozenset({"best", "top"})
_GATE_MODIFIERS = INTENT_MODIFIERS - _TOPIC_WORDS - _NEUTRAL_MODIFIERS


@dataclass(frozen=True)
class Keyword:
    """One keyword in a run, joined to its metrics row."""

    metric_id: str
    keyword_id: str
    text: str
    volume: int
    cluster: str = ""
    intent: str = ""


@dataclass
class Group:
    """A proposed cluster: member indices into the run's keyword list."""

    members: list[int]
    label: str = ""
    intent: str = ""
    source: str = ""


def _signature(text: str) -> frozenset[str]:
    """Every intent-bearing modifier in the query, e.g. {best, software}."""
    return frozenset(token for token in tokens(text) if token in INTENT_MODIFIERS)


def _gate(text: str) -> frozenset[str]:
    """The intent *axis* used to keep queries apart.

    Coarser than the raw signature on purpose: superlatives are dropped (they do
    not change the need) and topic words are not modifiers at all.
    """
    return frozenset(token for token in tokens(text) if token in _GATE_MODIFIERS)


def _intent_from_signatures(signatures: Iterable[frozenset[str]]) -> str:
    merged: set[str] = set()
    for signature in signatures:
        merged |= signature
    if merged & _COMPARISON:
        return "comparison"
    if merged & _COMMERCIAL:
        return "commercial"
    return "informational"


def _phrase_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _label(keywords: Sequence[Keyword], members: Sequence[int]) -> str:
    """Human topic label: the representative's shared (non-modifier) tokens."""
    rep = keywords[members[0]]
    counts: Counter[str] = Counter()
    for member in members:
        for token in set(content_tokens(keywords[member].text)):
            counts[token] += 1
    keep = [
        token
        for token in content_tokens(rep.text)
        if token not in INTENT_MODIFIERS and counts[token] * 2 >= len(members)
    ][:3]
    words = keep or [head_term(rep.text)]
    return " ".join(word for word in words if word).strip() or rep.text[:60]


def load_keywords(pb: Any, run_id: str) -> list[Keyword]:
    """Load a run's keywords, highest volume first, with their display text."""
    metrics = KeywordMetricRepo(pb)
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < MAX_KEYWORDS:
        batch = metrics.list_for_run(
            run_id, sort="-avgMonthlySearches", page=page, per_page=LOAD_PAGE
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < LOAD_PAGE:
            break
        page += 1
    if not rows:
        return []
    keyword_ids = [row.get("keyword", "") for row in rows]
    texts = KeywordRepo(pb).map_by_ids([kid for kid in keyword_ids if kid])
    keywords: list[Keyword] = []
    for row in rows[:MAX_KEYWORDS]:
        source = texts.get(row.get("keyword", "")) or {}
        text = source.get("displayKeyword") or source.get("normalizedKeyword") or ""
        if not text:
            continue
        keywords.append(
            Keyword(
                metric_id=row["id"],
                keyword_id=row.get("keyword", ""),
                text=text,
                volume=int(row.get("avgMonthlySearches") or 0),
                cluster=row.get("cluster", ""),
                intent=str(row.get("intent") or ""),
            )
        )
    return keywords


def deterministic_members(keywords: Sequence[Keyword]) -> list[list[int]]:
    """Greedy lexical clustering bounded to O(n·k).

    ``ponytail:`` each token bucket keeps only its CANDIDATE_CAP highest-volume
    members and a keyword only ever compares against that capped pool, so we
    never build the n×n similarity matrix. Raise CANDIDATE_CAP if recall turns
    out to matter more than cost.
    """
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, keyword in enumerate(keywords):  # already volume-descending
        for token in set(content_tokens(keyword.text)):
            bucket = buckets[token]
            if len(bucket) < CANDIDATE_CAP:
                bucket.append(index)

    signatures = [_gate(keyword.text) for keyword in keywords]
    assigned = [-1] * len(keywords)
    groups: list[list[int]] = []
    for seed in range(len(keywords)):
        if assigned[seed] >= 0:
            continue
        group_index = len(groups)
        assigned[seed] = group_index
        members = [seed]
        candidates: set[int] = set()
        for token in set(content_tokens(keywords[seed].text)):
            candidates.update(buckets.get(token, ()))
        scored: list[tuple[float, int]] = []
        for other in candidates:
            if other == seed or assigned[other] >= 0 or signatures[other] != signatures[seed]:
                continue
            score = similarity(keywords[seed].text, keywords[other].text)
            if score >= SIMILARITY_THRESHOLD:
                scored.append((score, other))
        scored.sort(reverse=True)
        for _score, other in scored[:CANDIDATE_CAP]:
            if assigned[other] >= 0:
                continue
            assigned[other] = group_index
            members.append(other)
        groups.append(members)
    return groups


def deterministic_groups(keywords: Sequence[Keyword]) -> list[Group]:
    return [Group(members=members) for members in deterministic_members(keywords)]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


async def embedding_members(
    provider: Any,
    keywords: Sequence[Keyword],
    *,
    threshold: float = EMBEDDING_THRESHOLD,
) -> list[list[int]]:
    """Greedy embedding clustering against cluster representatives.

    ``ponytail:`` a single pass comparing each keyword to the existing
    representatives (O(n·c)); swap for k-means/HDBSCAN only if measured cluster
    quality says so.
    """
    texts = [keyword.text for keyword in keywords]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        vectors.extend(await provider.embed_documents(texts[start : start + EMBED_BATCH]))
    if len(vectors) != len(texts):
        raise ProviderError(
            f"embedding provider returned {len(vectors)} vectors for {len(texts)} keywords"
        )

    signatures = [_gate(keyword.text) for keyword in keywords]
    groups: list[list[int]] = []
    reps: list[tuple[int, list[float]]] = []
    for index, vector in enumerate(vectors):
        best_group = -1
        best_score = 0.0
        for group_index, (rep, rep_vector) in enumerate(reps):
            if signatures[rep] != signatures[index]:
                continue
            score = _cosine(vector, rep_vector)
            if score > best_score:
                best_score, best_group = score, group_index
        if best_group >= 0 and best_score >= threshold:
            groups[best_group].append(index)
        else:
            reps.append((index, vector))
            groups.append([index])
    return groups


async def llm_groups(
    ctx: Any, keywords: Sequence[Keyword], *, batch_size: int = LLM_BATCH
) -> tuple[list[Group], list[str]]:
    """Batched LLM regrouping → (groups, non-fatal notes).

    The model only sees ids we sent and may only regroup them; unknown,
    malformed or repeated ids are discarded. Members the model leaves out (or a
    batch that fails entirely) fall back to lexical grouping, so no keyword is
    silently dropped.
    """
    service = PromptService(ctx.pb)
    system_template = ctx.config.prompt("cluster_system")
    user_template = ctx.config.prompt("cluster_user")
    role = ctx.config.role_llm("meta")
    llm = ctx.providers.llm_for("meta")
    params = GenerationParams(
        temperature=float(role.get("temperature") or 0.2),
        max_tokens=int(role.get("max_tokens") or 4096),
        timeout=float(role.get("timeout") or 120.0),
    )

    groups: list[Group] = []
    notes: list[str] = []
    for start in range(0, len(keywords), batch_size):
        indices = list(range(start, min(start + batch_size, len(keywords))))
        payload = [
            {
                "id": keywords[index].metric_id,
                "text": keywords[index].text,
                "volume": keywords[index].volume,
            }
            for index in indices
        ]
        by_id = {keywords[index].metric_id: index for index in indices}
        try:
            context = service.build_context(
                ctx.config,
                extra={"keywords": json.dumps(payload, ensure_ascii=False)},
            )
            system = service.render(system_template, context) if system_template else ""
            user = (
                service.render(user_template, context)
                if user_template
                else json.dumps({"keywords": payload}, ensure_ascii=False)
            )
            result = await llm.generate(system=system or None, user=user, params=params)
            data = extract_json(result.text)
            raw = data.get("clusters") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("cluster response contained no clusters list")
            used: set[int] = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                members = [
                    by_id[member_id]
                    for member_id in item.get("member_ids") or []
                    if member_id in by_id and by_id[member_id] not in used
                ]
                if not members:
                    continue
                used.update(members)
                intent = str(item.get("intent") or "").strip().lower()
                groups.append(
                    Group(
                        members=members,
                        label=str(item.get("label") or "").strip()[:LLM_LABEL_MAX],
                        intent=intent if intent in INTENTS else "",
                        source=LLM_SOURCE,
                    )
                )
            leftover = [index for index in indices if index not in used]
            if leftover:
                groups.extend(_deterministic_subset(keywords, leftover))
        except Exception as exc:  # one bad batch must not kill the run
            notes.append(
                f"llm clustering batch {start // batch_size + 1} failed: {type(exc).__name__}"
            )
            groups.extend(_deterministic_subset(keywords, indices))
    return groups, notes


def _deterministic_subset(keywords: Sequence[Keyword], indices: Sequence[int]) -> list[Group]:
    subset = [keywords[index] for index in indices]
    return [
        Group(members=[indices[position] for position in members])
        for members in deterministic_members(subset)
    ]


def fill_labels(keywords: Sequence[Keyword], groups: Sequence[Group]) -> list[Group]:
    """Complete labels/intents so every group is persistable and explainable."""
    for group in groups:
        if not group.members:
            continue
        if not group.label:
            group.label = _phrase_case(_label(keywords, group.members))
        if not group.intent:
            group.intent = _intent_from_signatures(
                _signature(keywords[member].text) for member in group.members
            )
        if not group.source:
            group.source = DETERMINISTIC_SOURCE
    return [group for group in groups if group.members]


def persist_clusters(
    pb: Any,
    *,
    run_id: str,
    project_id: str,
    keywords: Sequence[Keyword],
    groups: Sequence[Group],
    method: str,
    confidence: float = 0.0,
) -> dict[str, int]:
    """Upsert clusters by slug, reassign changed metrics, drop stale clusters.

    Re-running clustering on the same run is therefore idempotent: the same
    labels reuse the same rows and only genuinely moved keywords are written.
    """
    repo = ClusterRepo(pb)
    existing: dict[str, dict[str, Any]] = {}
    for cluster_row in repo.list_for_run(run_id):
        existing[cluster_row.get("slug") or ""] = cluster_row

    metrics = KeywordMetricRepo(pb)
    assigned = 0
    kept: set[str] = set()
    used_slugs: set[str] = set()
    for index, group in enumerate(groups):
        label = group.label or f"Cluster {index + 1}"
        slug = slugify(label, fallback=f"cluster-{index + 1}")
        # Distinct clusters can share a label (both "Gym management" while
        # differing by intent). Suffix repeats so they never collapse into one.
        if slug in used_slugs:
            suffix = 2
            while f"{slug}-{suffix}" in used_slugs:
                suffix += 1
            slug = f"{slug}-{suffix}"
        used_slugs.add(slug)
        payload = {
            "project": project_id,
            "run": run_id,
            "name": label,
            "slug": slug,
            "size": len(group.members),
            "primaryKeyword": keywords[group.members[0]].text[:500],
            "method": method,
            "confidence": confidence,
            "meta": {
                "intent": group.intent,
                "intentSource": group.source,
                "sample": [keywords[member].text for member in group.members[:8]],
            },
        }
        row = existing.get(slug)
        if row is None:
            row = repo.create({**payload, "status": "proposed"})
            existing[slug] = row
        else:
            repo.update(row["id"], payload)
        kept.add(slug)
        for member in group.members:
            keyword = keywords[member]
            changes: dict[str, Any] = {}
            if keyword.cluster != row["id"]:
                changes["cluster"] = row["id"]
            if not keyword.intent and group.intent and group.intent != "unknown":
                changes["intent"] = group.intent
                changes["intentSource"] = group.source
            if changes:
                metrics.update(keyword.metric_id, changes)
                assigned += 1

    removed = 0
    for slug, stale_row in existing.items():
        if slug not in kept:
            repo.delete(stale_row["id"])
            removed += 1
    return {"clusters": len(kept), "assigned": assigned, "removed": removed}


async def cluster_run(ctx: Any, *, run_id: str, method: str = "auto") -> dict[str, Any]:
    """Cluster one run's keywords and persist the result.

    ``method`` is ``auto`` (embedding when available, else lexical),
    ``deterministic``, ``embedding`` or ``llm``. A failing optional strategy
    degrades to lexical clustering with a visible note — a research run should
    not die because an embedding endpoint is unconfigured.
    """
    await ctx.check_cancelled()
    chosen = (method or "auto").strip().lower()
    if chosen not in METHODS:
        chosen = "auto"

    keywords = load_keywords(ctx.pb, run_id)
    if not keywords:
        return {
            "keywords": 0,
            "clusters": 0,
            "assigned": 0,
            "removed": 0,
            "method": chosen,
            "errors": [],
        }
    ctx.progress(0, stage="clustering", message=f"Clustering {len(keywords)} keywords…")

    notes: list[str] = []
    groups: list[Group] = []
    used = "deterministic"

    if chosen in ("auto", "embedding"):
        try:
            members = await embedding_members(ctx.providers.embedding, keywords)
            groups = [Group(members=member_list) for member_list in members]
            used = "embedding"
        except Exception as exc:
            notes.append(f"embedding clustering unavailable: {type(exc).__name__}")
            ctx.warning(
                "embedding clustering failed; using lexical clustering", {"error": str(exc)[:200]}
            )

    if chosen == "llm":
        groups, llm_notes = await llm_groups(ctx, keywords)
        notes.extend(llm_notes)
        used = "mixed" if llm_notes else "llm"

    if not groups:
        groups = deterministic_groups(keywords)
        used = "deterministic"

    groups = fill_labels(keywords, groups)
    result = persist_clusters(
        ctx.pb,
        run_id=run_id,
        project_id=ctx.project_id,
        keywords=keywords,
        groups=groups,
        method=used,
    )
    ctx.progress(
        100,
        stage="clustering",
        message=f"Clustered {len(keywords)} keywords into {result['clusters']} clusters",
    )
    ctx.info(
        "keyword clustering completed",
        {
            "keywords": len(keywords),
            "clusters": result["clusters"],
            "method": used,
            "errors": len(notes),
        },
    )
    return {**result, "keywords": len(keywords), "method": used, "errors": notes}


def _demo() -> None:
    keywords = [
        Keyword("m1", "k1", "gym management software", 8100),
        Keyword("m2", "k2", "best gym management software", 2400),
        Keyword("m3", "k3", "gym management software pricing", 880),
        Keyword("m4", "k4", "restaurant accounting software", 1300),
        Keyword("m5", "k5", "gym crm", 720),
        Keyword("m6", "k6", "how much does gym software cost", 320),
    ]
    members = deterministic_members(keywords)
    lookup = {tuple(group) for group in members}
    groups = fill_labels(keywords, deterministic_groups(keywords))

    assert _signature("best gym software") == frozenset({"best", "software"})
    assert _gate("best gym software") == frozenset()
    assert _gate("gym management software pricing") == frozenset({"pricing"})
    assert _intent_from_signatures([_signature("gym software vs trainer")]) == "comparison"
    assert _intent_from_signatures([_signature("best gym software")]) == "commercial"
    assert _intent_from_signatures([_signature("what is gym software")]) == "informational"
    # same intent axis + high overlap → one cluster (superlatives are neutral)
    assert any(set(group.members) == {0, 1} for group in groups)
    # pricing and cost-bearing queries keep their own cluster
    assert any(group.members == [2] for group in groups)
    assert any(group.members == [5] for group in groups)
    assert all(group.label and group.intent for group in groups)
    assert sorted(member for group in members for member in group) == list(range(len(keywords)))
    assert len(lookup) == len(members)


if __name__ == "__main__":
    _demo()
    print("research_clustering ok")
