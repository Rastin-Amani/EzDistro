"""Article opportunity engine.

Turns a finished research dataset (keywords + clusters + competitor pages +
existing site content) plus the user's own description of what they want into
reviewable article ideas.

Facts first: the engine never invents search volume, competition, CPC or
rankings — those come from collected data only. The AI step is given the
research evidence as context and may only choose among the keywords it was
handed, classify intent, pick the right action and write the angle.

Design rules that matter here:

- batched, never one giant prompt (the spec explicitly forbids "give me 10,000
  articles"), and a failed batch is retried on its own, not by restarting;
- deterministic wherever a rule works (intent fallback, action fallback,
  duplicate detection, scoring) so the AI is spent only where it adds value;
- the opportunity score is transparent and versioned: every component is stored
  on the row, and live-SERP signals are excluded (not faked) when no SERP
  provider is configured.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.keywords import content_tokens, normalize_keyword, similarity
from app.domain.parsing import extract_json
from app.providers.base import GenerationParams
from app.repositories.research import (
    ArticleIdeaRepo,
    ClusterRepo,
    CompetitorPageRepo,
    ContentGapRepo,
    KeywordMetricRepo,
    KeywordRepo,
    q,
)
from app.services.prompt_service import PromptService

SCORE_VERSION = "opp_v1"
GOAL_SOURCE = "goal_heuristic_v1"
LLM_SOURCE = "opportunity_v1"

LLM_CLUSTERS_PER_BATCH = 6
KEYWORDS_PER_CLUSTER = 12
COMPETITORS_PER_CLUSTER = 8
MAX_OPPORTUNITIES = 500
MAX_CANDIDATES = 400

# Similarity at which a candidate is considered to describe an existing page
# rather than a new one. Above this the action is forced to ``update``.
CANNIBALIZATION_THRESHOLD = 0.66
# Similarity at which two candidates in the same run are the same idea.
DUPLICATE_THRESHOLD = 0.78

ACTIONS = ("generate", "update", "expand", "support", "reject")
INTENTS = (
    "informational",
    "commercial",
    "transactional",
    "navigational",
    "local",
    "comparison",
    "unknown",
)
CONTENT_TYPES = (
    "guide",
    "tutorial",
    "how-to",
    "comparison",
    "alternatives",
    "best-of list",
    "review",
    "problem/solution",
    "glossary",
    "faq",
    "case study",
    "template",
    "checklist",
    "statistics",
    "commercial supporting article",
)

SCORE_WEIGHTS: dict[str, float] = {
    "demand": 0.30,
    "business": 0.25,
    "gap": 0.20,
    "coverage": 0.15,
    "uniqueness": 0.10,
}

_OBJECTIVE_PHRASES: dict[str, tuple[str, ...]] = {
    "leads": ("lead", "leads", "demo", "demos", "signup", "sign up", "trial", "qualified"),
    "sales": ("sales", "revenue", "sell", "buyers", "purchase", "customers"),
    "authority": ("authority", "topical", "pillar", "roadmap", "comprehensive"),
    "traffic": ("traffic", "visitors", "sessions", "rank", "rankings", "visibility"),
    "updates": ("update", "updates", "refresh", "existing content", "stale", "outdated"),
}

_INTENT_HINTS: dict[str, tuple[str, ...]] = {
    "commercial": ("high-intent", "high intent", "commercial", "buyer"),
    "comparison": ("comparison", "compare", "alternatives"),
    "transactional": ("transactional", "pricing", "sign up", "trial"),
    "informational": ("how to", "guide", "educational", "informational"),
    "local": ("local", "near me"),
}

_MIN_VOLUME_RE = re.compile(
    r"(?:at least|minimum|min\.?|above|over|more than|>=)\s*([\d,]+)", re.IGNORECASE
)
_MAX_VOLUME_RE = re.compile(
    r"(?:below|under|less than|at most|maximum|no more than|<=)\s*([\d,]+)", re.IGNORECASE
)
_MAX_IDEAS_RE = re.compile(r"([\d,]+)\s*(?:opportunit|article|idea|post|page)", re.IGNORECASE)
_EXCLUDE_RE = re.compile(
    r"(?:avoid|exclude|skip|do not (?:write about|cover|target)|don't (?:write about|cover|target))"
    r"\s+((?:[^.;,\d]|,?\s+(?!at least|above|over|more than|below|under|less than|up to)\d)+)",
    re.IGNORECASE,
)
_EXCLUDE_MAX = 60


# ---------------------------------------------------------------------------
# Goal parsing
# ---------------------------------------------------------------------------
_GENERIC_EXCLUDE_WORDS = frozenset(
    {
        "about",
        "article",
        "articles",
        "content",
        "keyword",
        "keywords",
        "page",
        "pages",
        "post",
        "posts",
        "related",
        "topic",
        "topics",
    }
)


@dataclass
class Goal:
    """Structured form of the user's natural-language goal.

    The AI is not asked to re-read the prose on every batch: the goal is parsed
    once into constraints the deterministic scorer and filters can apply.
    """

    text: str = ""
    audience: str = ""
    objectives: list[str] = field(default_factory=list)
    priority_intents: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    preferred_content_types: list[str] = field(default_factory=list)
    min_volume: int = 0
    max_volume: int = 0
    max_opportunities: int = MAX_OPPORTUNITIES
    include_supporting: bool = True
    source: str = GOAL_SOURCE

    def allows(self, text: str) -> bool:
        """Whether an excluded term rules this text out.

        Exclusion is topic-level: the generic filler the user wraps around a
        banned topic ("avoid crypto **topics**") is dropped, then a ban applies
        when any remaining term is present.
        """
        haystack = normalize_keyword(text)
        tokens = set(content_tokens(text))
        for term in self.excluded:
            if not term:
                continue
            significant = [
                token for token in content_tokens(term) if token not in _GENERIC_EXCLUDE_WORDS
            ] or [term]
            if any(f" {token} " in f" {haystack} " for token in significant) or all(
                token in tokens for token in significant
            ):
                return False
        return True


def _int(value: str) -> int:
    return int(value.replace(",", "").strip() or 0)


def parse_goal(
    text: str,
    *,
    structured: dict[str, Any] | None = None,
    default_max: int = MAX_OPPORTUNITIES,
) -> Goal:
    """Parse the goal into constraints. Heuristic, and said so in the data."""
    data = structured or {}
    raw = (text or "").strip()
    haystack = raw.lower()

    objectives = [
        name
        for name, phrases in _OBJECTIVE_PHRASES.items()
        if any(phrase in haystack for phrase in phrases)
    ]
    priority_intents = [
        intent
        for intent, phrases in _INTENT_HINTS.items()
        if any(phrase in haystack for phrase in phrases)
    ]
    excluded = [part.strip() for part in _EXCLUDE_RE.findall(raw)]
    excluded = [item for chunk in excluded for item in re.split(r",| and ", chunk)]
    excluded = [
        normalize_keyword(item)
        for item in excluded
        if 2 < len(item.strip()) <= _EXCLUDE_MAX and not any(c.isdigit() for c in item)
    ]

    found_min = _MIN_VOLUME_RE.search(raw)
    found_max = _MAX_VOLUME_RE.search(raw)
    min_volume = _int(found_min.group(1)) if found_min else 0
    max_volume = _int(found_max.group(1)) if found_max else 0
    found = _MAX_IDEAS_RE.search(raw)
    limit = _int(found.group(1)) if found else default_max

    goal = Goal(
        text=raw,
        audience=str(data.get("audience") or ""),
        objectives=objectives,
        priority_intents=priority_intents,
        excluded=excluded,
        preferred_content_types=[str(v).lower() for v in data.get("contentTypes") or []],
        min_volume=max(min_volume, int(data.get("minVolume") or 0)),
        max_volume=max(max_volume, int(data.get("maxVolume") or 0)),
        max_opportunities=max(1, int(data.get("maxOpportunities") or limit)),
        include_supporting=bool(data.get("includeSupporting", True)),
    )
    for intent in data.get("priorityIntents") or []:
        if intent in INTENTS and intent not in goal.priority_intents:
            goal.priority_intents.append(intent)
    for term in data.get("excludedTopics") or []:
        normalized = normalize_keyword(str(term))
        if normalized and normalized not in goal.excluded:
            goal.excluded.append(normalized)
    # A goal that asks only for updates should not be padded with fresh pillars.
    if "updates" in objectives and not goal.priority_intents:
        goal.priority_intents = ["informational", "commercial"]
    return goal


# ---------------------------------------------------------------------------
# Candidate retrieval — never an n×n similarity matrix
# ---------------------------------------------------------------------------
class MatchIndex:
    """Token-bucketed similarity lookup over (text, row) pairs.

    Only rows sharing at least one content token with the query are compared, so
    a 10,000-keyword run stays O(n·k) instead of building the full matrix.
    """

    def __init__(self, entries: Iterable[tuple[str, dict[str, Any]]] = ()) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self._index: dict[str, list[int]] = defaultdict(list)
        for text, row in entries:
            self.add(text, row)

    def add(self, text: str, row: dict[str, Any]) -> None:
        if not text.strip():
            return
        self.items.append((text, row))
        position = len(self.items) - 1
        for token in set(content_tokens(text)):
            self._index[token].append(position)

    def best(self, text: str, *, threshold: float = 0.0) -> tuple[dict[str, Any] | None, float]:
        candidates: set[int] = set()
        for token in set(content_tokens(text)):
            bucket = self._index.get(token)
            if bucket:
                candidates.update(bucket)
        best_row: dict[str, Any] | None = None
        best_score = 0.0
        for position in list(candidates)[:MAX_CANDIDATES]:
            score = similarity(text, self.items[position][0])
            if score > best_score:
                best_score = score
                best_row = self.items[position][1]
        if best_score < threshold:
            return None, best_score
        return best_row, best_score


# ---------------------------------------------------------------------------
# Content gaps
# ---------------------------------------------------------------------------
def _coverage(match_index: MatchIndex, label: str, sample: Sequence[str]) -> int:
    """How many pages of one side address this cluster.

    Counted from observed content only (titles/headings we actually fetched) —
    it is a coverage count, not a ranking claim.
    """
    hits: set[int] = set()
    for text in (label, *sample):
        if not text:
            continue
        row, score = match_index.best(text, threshold=0.5)
        if row is not None:
            hits.add(id(row))
            del score
    return len(hits)


def compute_content_gaps(
    pb: Any,
    *,
    run_id: str,
    project_id: str,
    max_keywords_per_cluster: int = 60,
) -> int:
    """Compare your content vs competitor content vs the keyword universe."""
    from app.repositories.articles import ArticleRepo

    metrics = _load_metrics(pb, run_id)
    clusters = ClusterRepo(pb).map_for_run(run_id)
    texts = KeywordRepo(pb).map_by_ids([row.get("keyword", "") for row in metrics])
    top_gaps = ContentGapRepo(pb)

    articles = ArticleRepo(pb).list_all(
        filter=f"project={q(project_id)}",
        fields="id,title,slug,wordpressUrl,source,status",
    )
    competitors = CompetitorPageRepo(pb).list_all(filter=f"run={q(run_id)}")
    your_index = MatchIndex((row.get("title") or "", row) for row in articles)
    competitor_index = MatchIndex(
        (
            " ".join([row.get("title") or "", row.get("h1") or ""]).strip(),
            row,
        )
        for row in competitors
    )

    rows: list[dict[str, Any]] = []
    for cluster_id, group in _by_cluster(metrics, texts).items():
        cluster = clusters.get(cluster_id) or {}
        label = cluster.get("name") or ""
        sample = [item["text"] for item in group[:max_keywords_per_cluster]]
        demand = sum(item["volume"] for item in group)
        your_coverage = _coverage(your_index, label, sample[:12])
        competitor_coverage = _coverage(competitor_index, label, sample[:12])
        if not your_coverage and not competitor_coverage:
            gap_type = "under_served"
        elif competitor_coverage and not your_coverage:
            gap_type = "competitor_only"
        elif your_coverage and not competitor_coverage:
            gap_type = "you_only"
        elif competitor_coverage > your_coverage * 2:
            gap_type = "under_served"
        else:
            gap_type = "both"
        gap_score = _gap_score(demand, competitor_coverage, your_coverage)
        rows.append(
            {
                "project": project_id,
                "run": run_id,
                "gapType": gap_type,
                "cluster": cluster_id,
                "keyword": cluster.get("primaryKeyword") or (sample[0] if sample else ""),
                "demand": demand,
                "competitorCoverage": competitor_coverage,
                "yourCoverage": your_coverage,
                "score": gap_score,
                "competitorPages": [
                    row.get("canonicalUrl") or row.get("url") or ""
                    for row in competitors[:10]
                    if row.get("status") == "fetched"
                ],
                "notes": {
                    "keywords": sample[:10],
                    "source": "observed",
                },
            }
        )
    return top_gaps.replace_for_run(run_id, rows)


def _gap_score(demand: int, competitor_coverage: int, your_coverage: int) -> float:
    demand_score = min(1.0, math.log1p(max(demand, 0)) / math.log1p(50_000))
    evidence = min(1.0, competitor_coverage / 5.0)
    covered = min(1.0, your_coverage / 5.0)
    return round(100 * (0.6 * demand_score + 0.4 * evidence) * (1.0 - 0.6 * covered), 1)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _demand_score(volume: int) -> float:
    return min(1.0, math.log1p(max(volume, 0)) / math.log1p(20_000))


def _business_score(goal: Goal, *, intent: str, action: str, content_type: str) -> float:
    score = 0.4
    if intent and intent in goal.priority_intents:
        score += 0.35
    if action in ("update", "expand") and "updates" in goal.objectives:
        score += 0.2
    if content_type and content_type in goal.preferred_content_types:
        score += 0.15
    if {"leads", "sales"} & set(goal.objectives) and intent in (
        "commercial",
        "comparison",
        "transactional",
    ):
        score += 0.2
    if "authority" in goal.objectives and action in ("generate", "support"):
        score += 0.1
    return max(0.0, min(1.0, score))


def score_opportunity(
    *,
    volume: int,
    intent: str,
    action: str,
    content_type: str,
    goal: Goal,
    gap_score: float = 0.5,
    existing_similarity: float = 0.0,
    uniqueness: float = 1.0,
) -> tuple[float, dict[str, Any]]:
    """Transparent, versioned, multi-component opportunity score (0-100).

    Component weights are centralised in ``SCORE_WEIGHTS``. Live-SERP signals
    are not a component at all: with no SERP provider there is nothing to
    measure, and a fabricated weight would be worse than a missing one.
    """
    components = {
        "demand": round(_demand_score(volume), 3),
        "business": round(
            _business_score(goal, intent=intent, action=action, content_type=content_type), 3
        ),
        "gap": round(max(0.0, min(1.0, gap_score / 100.0)), 3),
        "coverage": round(max(0.0, 1.0 - existing_similarity), 3),
        "uniqueness": round(max(0.0, min(1.0, uniqueness)), 3),
    }
    total = sum(SCORE_WEIGHTS[key] * value for key, value in components.items())
    detail = {
        "version": SCORE_VERSION,
        "weights": SCORE_WEIGHTS,
        "components": components,
        "serp": None,
        "notes": ["live SERP signals unavailable — excluded, not estimated"],
    }
    return round(total * 100, 1), detail


def _confidence(score: float, volume: int) -> str:
    if score >= 65 and volume >= 500:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_metrics(pb: Any, run_id: str) -> list[dict[str, Any]]:
    repo = KeywordMetricRepo(pb)
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = repo.list_for_run(run_id, sort="-avgMonthlySearches", page=page, per_page=500)
        rows.extend(chunk)
        if len(chunk) < 500:
            return rows
        page += 1


def _by_cluster(
    metrics: Sequence[dict[str, Any]], texts: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in metrics:
        cluster_id = row.get("cluster") or ""
        if not cluster_id:
            continue
        keyword_row = texts.get(row.get("keyword") or "") or {}
        text = keyword_row.get("displayKeyword") or keyword_row.get("normalizedKeyword") or ""
        if not text:
            continue
        grouped[cluster_id].append(
            {
                "metricId": row["id"],
                "keywordId": row.get("keyword") or "",
                "text": text,
                "volume": int(row.get("avgMonthlySearches") or 0),
                "competition": row.get("competition") or "",
                "competitionIndex": int(row.get("competitionIndex") or 0),
                "intent": row.get("intent") or "",
            }
        )
    for group in grouped.values():
        group.sort(key=lambda item: -item["volume"])
    return grouped


# ---------------------------------------------------------------------------
# Prompt context plumbing
# ---------------------------------------------------------------------------
def _keyword_lookup(group: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {normalize_keyword(item["text"]): item for item in group}


def _resolve_keyword(value: str, lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Match an AI-returned keyword back to a keyword we actually collected."""
    normalized = normalize_keyword(value)
    if not normalized:
        return None
    if normalized in lookup:
        return lookup[normalized]
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in lookup.values():
        score = similarity(normalized, item["text"])
        if score > best_score:
            best_score = score
            best = item
    return best if best_score >= 0.6 else None


def _normalize_intent(value: str, fallback: str = "unknown") -> str:
    intent = str(value or "").strip().lower()
    return intent if intent in INTENTS else fallback


def _normalize_content_type(value: str) -> str:
    content_type = str(value or "").strip().lower()
    return content_type if content_type in CONTENT_TYPES else ""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
async def generate_opportunities(
    ctx: Any,
    *,
    run_id: str,
    goal_text: str,
    structured_goal: dict[str, Any] | None = None,
    max_opportunities: int = 0,
) -> dict[str, Any]:
    """Batched, deduplicated, resumable opportunity generation.

    Resume rule: clusters that already produced ideas in this run are skipped, so
    a crashed run costs at most one batch of AI tokens.
    """
    await ctx.check_cancelled()
    goal = parse_goal(goal_text, structured=structured_goal)
    limit = max_opportunities or goal.max_opportunities
    locales = ctx.config.settings.get("localization") or {}
    locale = str(locales.get("targetLocale") or "")
    language = ctx.config.language
    if not goal.audience:
        goal.audience = str(locales.get("targetAudience") or "")

    metrics = _load_metrics(ctx.pb, run_id)
    if not metrics:
        return {
            "clusters": 0,
            "batches": 0,
            "created": 0,
            "skipped": 0,
            "duplicates": 0,
            "updates": 0,
            "errors": ["no keywords in this run — clustering must run first"],
            "goal": _goal_dict(goal),
        }
    clusters = ClusterRepo(ctx.pb).map_for_run(run_id)
    grouped = _by_cluster(
        metrics, KeywordRepo(ctx.pb).map_by_ids([r.get("keyword", "") for r in metrics])
    )
    idea_repo = ArticleIdeaRepo(ctx.pb)

    done_label = {
        row.get("cluster")
        for row in idea_repo.list_all(filter=f'run={q(run_id)} && status="proposed"')
    }
    grouped = {key: value for key, value in grouped.items() if key not in done_label}

    from app.repositories.articles import ArticleRepo

    articles = ArticleRepo(ctx.pb).list_all(
        filter=f"project={q(ctx.project_id)}",
        fields="id,title,slug,wordpressUrl,wordpressPostId,source,status",
    )
    existing = MatchIndex((row.get("title") or "", row) for row in articles)
    competitors = CompetitorPageRepo(ctx.pb).list_all(filter=f"run={q(run_id)}")
    gaps = {
        row.get("cluster"): row
        for row in ContentGapRepo(ctx.pb).list_all(filter=f"run={q(run_id)}")
    }

    service = PromptService(ctx.pb)
    system_template = ctx.config.prompt("opportunity_system")
    user_template = ctx.config.prompt("opportunity_user")
    role = ctx.config.role_llm("meta")
    llm = ctx.providers.llm_for("meta")
    params = GenerationParams(
        temperature=float(role.get("temperature") or 0.3),
        max_tokens=int(role.get("max_tokens") or 4096),
        timeout=float(role.get("timeout") or 120.0),
    )
    prompt_version = ctx.config.prompt_version("opportunity_system")

    order = sorted(grouped.items(), key=lambda item: -sum(k["volume"] for k in item[1]))
    accepted = MatchIndex()
    seen: list[dict[str, Any]] = []
    errors: list[str] = []
    created = duplicates = updates = skipped = batches = 0

    for start in range(0, len(order), LLM_CLUSTERS_PER_BATCH):
        if created >= limit:
            break
        await ctx.check_cancelled()
        batch = order[start : start + LLM_CLUSTERS_PER_BATCH]
        payload_clusters = []
        cluster_by_label: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for cluster_id, group in batch:
            cluster = clusters.get(cluster_id) or {}
            label = cluster.get("name") or group[0]["text"]
            members = group[:KEYWORDS_PER_CLUSTER]
            cluster_by_label[label] = (cluster_id, group)
            payload_clusters.append(
                {
                    "cluster": label,
                    "intent": cluster.get("meta", {}).get("intent", "") if cluster else "",
                    "gap": (gaps.get(cluster_id) or {}).get("gapType") or "",
                    "keywords": [
                        {"keyword": item["text"], "volume": item["volume"]} for item in members
                    ],
                    "existing": _existing_for(cluster_id, group, existing, articles),
                    "competitors": _competitors_for(group, competitors),
                }
            )
        try:
            context = service.build_context(
                ctx.config,
                extra={
                    "business_goal": goal.text,
                    "keywords": json.dumps(payload_clusters, ensure_ascii=False),
                    "existing_content": json.dumps(
                        [row.get("title") for row in articles[:40]], ensure_ascii=False
                    ),
                    "competitor_pages": json.dumps(
                        [
                            {
                                "url": row.get("canonicalUrl") or row.get("url"),
                                "title": row.get("title"),
                            }
                            for row in competitors[:40]
                        ],
                        ensure_ascii=False,
                    ),
                    "audience": goal.audience,
                    "language": language,
                    "locale": locale,
                },
            )
            system = service.render(system_template, context) if system_template else ""
            user = (
                service.render(user_template, context)
                if user_template
                else json.dumps({"clusters": payload_clusters}, ensure_ascii=False)
            )
            result = await llm.generate(system=system or None, user=user, params=params)
            data = extract_json(result.text)
            raw = data.get("opportunities") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("opportunity response contained no opportunities list")
        except Exception as exc:  # one bad batch must not kill the run
            errors.append(
                f"opportunity batch {start // LLM_CLUSTERS_PER_BATCH + 1} failed: {type(exc).__name__}"
            )
            ctx.warning("opportunity batch failed", {"error": str(exc)[:200]})
            continue

        batches += 1
        rows: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or created + len(rows) >= limit:
                continue
            row, reason = _build_row(
                item,
                cluster_by_label=cluster_by_label,
                existing=existing,
                goal=goal,
                gaps=gaps,
                locale=locale,
                language=language,
                prompt_version=prompt_version,
                model=getattr(llm, "model_name", ""),
                provider=getattr(llm, "provider_name", ""),
            )
            if row is None:
                skipped += 1
                if reason != "duplicate":
                    errors.append(f"skipped opportunity: {reason}")
                else:
                    duplicates += 1
                continue
            row["project"] = ctx.project_id
            row["run"] = run_id
            dup_row, dup_score = accepted.best(row["title"], threshold=DUPLICATE_THRESHOLD)
            if dup_row is not None:
                duplicates += 1
                _merge_duplicate(dup_row, row, similarity=dup_score)
                continue
            row["uniquenessScore"] = round(max(0.0, 1.0 - _seen_similarity(seen, row)), 3)
            row["opportunityScore"], row["scoreComponents"] = score_opportunity(
                volume=row["searchVolume"],
                intent=row["intent"],
                action=row["action"],
                content_type=row["contentType"],
                goal=goal,
                gap_score=float(row.pop("_gapScore")),
                existing_similarity=float(row.pop("_existingSimilarity")),
                uniqueness=row["uniquenessScore"],
            )
            row["confidence"] = _confidence(row["opportunityScore"], row["searchVolume"])
            seen.append(row)
            accepted.add(row["title"], row)
            rows.append(row)
            if row["action"] == "update":
                updates += 1

        if rows:
            idea_repo.create_many(rows)
            created += len(rows)
        ctx.progress(
            min(95, int(100 * (start + len(batch)) / max(1, len(order)))),
            stage="opportunities",
            message=f"Generating article opportunities ({start + len(batch)}/{len(order)} clusters)…",
            current=created,
        )

    ctx.info(
        "opportunity generation completed",
        {"created": created, "duplicates": duplicates, "skipped": skipped, "batches": batches},
    )
    return {
        "clusters": len(order),
        "batches": batches,
        "created": created,
        "skipped": skipped,
        "duplicates": duplicates,
        "updates": updates,
        "errors": errors[:20],
        "goal": _goal_dict(goal),
    }


def _existing_for(
    cluster_id: str,
    group: Sequence[dict[str, Any]],
    existing: MatchIndex,
    articles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Existing pages that look related to this cluster, for prompt context only."""
    hits: dict[str, dict[str, Any]] = {}
    for text in [group[0]["text"], *[item["text"] for item in group[:4]]]:
        row, score = existing.best(text, threshold=0.5)
        if row is not None and score >= 0.5:
            hits[row["id"]] = {
                "title": row.get("title"),
                "url": row.get("wordpressUrl") or "",
                "source": row.get("source") or "generated",
            }
    del cluster_id, articles
    return list(hits.values())[:5]


def _competitors_for(
    group: Sequence[dict[str, Any]], competitors: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    texts = [item["text"] for item in group[:KEYWORDS_PER_CLUSTER]]
    scored: list[tuple[float, dict[str, Any]]] = []
    for page in competitors:
        if page.get("status") != "fetched":
            continue
        haystack = f"{page.get('title') or ''} {page.get('h1') or ''}"
        best = max((similarity(text, haystack) for text in texts), default=0.0)
        if best >= 0.4:
            scored.append(
                (
                    best,
                    {
                        "url": page.get("canonicalUrl") or page.get("url"),
                        "title": page.get("title"),
                        "headings": (page.get("headings") or [])[:6],
                    },
                )
            )
    scored.sort(key=lambda item: -item[0])
    return [entry for _, entry in scored[:COMPETITORS_PER_CLUSTER]]


def _seen_similarity(seen: Sequence[dict[str, Any]], row: dict[str, Any]) -> float:
    best = 0.0
    for other in seen[-MAX_CANDIDATES:]:
        score = max(
            similarity(row["title"], other["title"]),
            similarity(row["primaryKeyword"], other["primaryKeyword"]),
        )
        if score > best:
            best = score
    return best


def _merge_duplicate(kept: dict[str, Any], duplicate: dict[str, Any], *, similarity: float) -> None:
    """Fold a near-identical candidate into the one we keep (no duplicate pages)."""
    secondary = list(kept.get("secondaryKeywords") or [])
    candidate = duplicate.get("primaryKeyword")
    if candidate and candidate not in secondary:
        secondary.append(candidate)
    for keyword in duplicate.get("secondaryKeywords") or []:
        if keyword not in secondary:
            secondary.append(keyword)
    kept["secondaryKeywords"] = secondary[:25]
    evidence = dict(kept.get("evidence") or {})
    merged = list(evidence.get("merged") or [])
    merged.append({"title": duplicate.get("title"), "similarity": round(similarity, 3)})
    evidence["merged"] = merged[:10]
    kept["evidence"] = evidence


def _build_row(
    item: dict[str, Any],
    *,
    cluster_by_label: dict[str, tuple[str, list[dict[str, Any]]]],
    existing: MatchIndex,
    goal: Goal,
    gaps: dict[Any, dict[str, Any]],
    locale: str,
    language: str,
    prompt_version: int,
    model: str,
    provider: str,
) -> tuple[dict[str, Any] | None, str]:
    """Validate one model-proposed opportunity and turn it into an article_ideas row."""
    title = str(item.get("title") or "").strip()
    if not title:
        return None, "missing title"

    cluster_id, group = _pick_cluster(item, cluster_by_label)
    lookup = _keyword_lookup(group)
    primary = _resolve_keyword(str(item.get("primary_keyword") or ""), lookup)
    if primary is None:
        return None, "primary keyword not in the collected keyword set"
    if goal.min_volume and primary["volume"] < goal.min_volume:
        return None, "below the minimum volume the user asked for"
    if goal.max_volume and primary["volume"] > goal.max_volume:
        return None, "above the maximum volume the user asked for"
    if not goal.allows(f"{title} {primary['text']}"):
        return None, "excluded by the user's goal"

    secondary: list[str] = []
    for value in item.get("secondary_keywords") or []:
        resolved = _resolve_keyword(str(value), lookup)
        if resolved is not None and resolved["text"] != primary["text"]:
            secondary.append(resolved["text"])
        if len(secondary) >= 15:
            break

    existing_row, existing_similarity = existing.best(primary["text"])
    title_row, title_similarity = existing.best(title)
    if title_row is not None and title_similarity > existing_similarity:
        existing_row, existing_similarity = title_row, title_similarity

    action = str(item.get("action") or "").strip().lower()
    if action not in ACTIONS:
        action = "update" if existing_row is not None else "generate"
    if existing_row is not None and existing_similarity >= CANNIBALIZATION_THRESHOLD:
        # A different title does not make it a new page.
        action = "reject" if action == "reject" else "update"

    cluster_row = gaps.get(cluster_id) or {}
    gap_score = float(cluster_row.get("score") or 50.0)
    intent = _normalize_intent(
        str(item.get("intent") or ""),
        fallback=_normalize_intent(str(cluster_row.get("intent") or "")),
    )
    content_type = _normalize_content_type(str(item.get("content_type") or ""))

    questions = [str(value)[:300] for value in (item.get("questions") or []) if str(value).strip()]
    entities = [str(value)[:120] for value in (item.get("entities") or []) if str(value).strip()]

    return (
        {
            "project": "",
            "run": "",
            "title": title[:1000],
            "suggestedTitle": str(item.get("title") or "")[:1000],
            "primaryKeyword": primary["text"][:500],
            "secondaryKeywords": secondary,
            "cluster": cluster_id,
            "intent": intent,
            "contentType": content_type,
            "action": action,
            "actionConfidence": 0.7 if action != "reject" else 0.4,
            "searchVolume": primary["volume"],
            "googleAdsCompetition": primary["competition"],
            "googleAdsCompetitionIndex": primary["competitionIndex"],
            "contentGapScore": gap_score,
            "businessRelevanceScore": round(
                _business_score(goal, intent=intent, action=action, content_type=content_type)
                * 100,
                1,
            ),
            "coverageScore": round(max(0.0, 1.0 - existing_similarity) * 100, 1),
            "recommendedAngle": str(item.get("angle") or "")[:4000],
            "uniqueValueProposition": str(item.get("unique_value") or "")[:4000],
            "targetAudience": str(item.get("audience") or goal.audience)[:1000],
            "contentBrief": str(item.get("brief") or "")[:20000],
            "questions": questions[:15],
            "entities": entities[:25],
            "internalLinks": [],
            "existingArticle": (existing_row or {}).get("id") or "",
            "canonicalExistingUrl": (existing_row or {}).get("wordpressUrl") or "",
            "evidence": {
                "source": "ai",
                "promptVersion": prompt_version,
                "model": model,
                "provider": provider,
                "clusterId": cluster_id,
                "keywords": [primary["text"], *secondary[:4]],
                "existingSimilarity": round(existing_similarity, 3),
                "audienceFit": str(item.get("audience_fit") or "")[:500],
                "serp": "unavailable",
            },
            "locale": locale,
            "language": language,
            "status": "proposed",
            "scoreVersion": SCORE_VERSION,
            "_gapScore": gap_score,
            "_existingSimilarity": existing_similarity,
        },
        "",
    )


def _pick_cluster(
    item: dict[str, Any], cluster_by_label: dict[str, tuple[str, list[dict[str, Any]]]]
) -> tuple[str, list[dict[str, Any]]]:
    label = str(item.get("cluster") or "").strip()
    if label in cluster_by_label:
        return cluster_by_label[label]
    for known, value in cluster_by_label.items():
        if label and similarity(label, known) >= 0.7:
            return value
    # No usable cluster from the model: attach to the batch's largest cluster so
    # the idea still carries a real provenance pointer.
    if not cluster_by_label:
        return "", []
    return max(cluster_by_label.values(), key=lambda value: sum(k["volume"] for k in value[1]))


def _goal_dict(goal: Goal) -> dict[str, Any]:
    return {
        "text": goal.text,
        "audience": goal.audience,
        "objectives": goal.objectives,
        "priorityIntents": goal.priority_intents,
        "excluded": goal.excluded,
        "minVolume": goal.min_volume,
        "maxVolume": goal.max_volume,
        "maxOpportunities": goal.max_opportunities,
        "source": goal.source,
    }


def _demo() -> None:
    goal = parse_goal(
        "I want qualified US leads from gym owners, high-intent searches, "
        "avoid crypto topics, at least 200 searches, up to 300 opportunities."
    )
    assert "leads" in goal.objectives, goal.objectives
    assert "commercial" in goal.priority_intents, goal.priority_intents
    assert goal.min_volume == 200, goal.min_volume
    assert goal.max_opportunities == 300, goal.max_opportunities
    assert goal.excluded, goal.excluded
    assert not goal.allows("crypto trading guide"), goal.excluded
    assert goal.allows("gym management software"), goal.excluded

    score, detail = score_opportunity(
        volume=8100,
        intent="commercial",
        action="generate",
        content_type="comparison",
        goal=goal,
        gap_score=80.0,
        existing_similarity=0.0,
    )
    weak, _ = score_opportunity(
        volume=10,
        intent="informational",
        action="reject",
        content_type="",
        goal=goal,
        gap_score=10.0,
        existing_similarity=0.9,
    )
    assert score > weak, (score, weak)
    assert 0 <= score <= 100 and 0 <= weak <= 100
    assert detail["serp"] is None and "components" in detail

    index = MatchIndex(
        [("Best Gym Management Software", {"id": "a"}), ("Gym Marketing Ideas", {"id": "b"})]
    )
    row, hit = index.best("best gym management software")
    assert row and row["id"] == "a" and hit >= CANNIBALIZATION_THRESHOLD, (row, hit)
    assert index.best("organic chemistry notes")[0] is None

    kept: dict[str, Any] = {"secondaryKeywords": [], "evidence": {}}
    _merge_duplicate(kept, {"title": "T", "primaryKeyword": "gym crm"}, similarity=0.9)
    assert kept["secondaryKeywords"] == ["gym crm"], kept
    assert _normalize_intent("Commercial") == "commercial"
    assert _normalize_intent("nonsense") == "unknown"
    assert _normalize_content_type("Guide") == "guide"
    assert _normalize_content_type("nonsense") == ""
    print("research_opportunities ok")


if __name__ == "__main__":
    _demo()
