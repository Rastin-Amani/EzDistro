# EzDistro Platform — Documentation

This folder is the documentation set for the EzDistro SEO-automation platform
(WordPress indexer + LLM article writer). It is **tracked** in the repository, so
these files ship with the codebase and are copied into the Docker image
(`COPY docs ./docs` in the Dockerfile).

```text
All documents verified against the working tree (research engine),
2026-10-04. Each file carries its own "Documentation status" block.
Conflict findings discovered during earlier audits are recorded honestly in
ARCHITECTURE §11 and SCHEMA §5.
The whole set is also served in-app at /help/ (auth-gated) — the Research tab's
"Setup guide" link points at /help/SEO_RESEARCH.md.
```

## Which document should I read?

| I am… | Start here | Then |
|---|---|---|
| A developer integrating / extending the platform | [ARCHITECTURE.md](ARCHITECTURE.md) | [SCHEMA.md](SCHEMA.md) · [CONFIGURATION.md](CONFIGURATION.md) |
| An operator running the service | [OPERATIONS.md](OPERATIONS.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| A content/SEO operator using the UI | [USER-GUIDE.md](USER-GUIDE.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Debugging a failed job | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | [FAILURES.md](FAILURES.md) |
| Wanting the failure-mode matrix | [FAILURES.md](FAILURES.md) | — |
| Building/using SEO research (Google Ads, competitors, opportunities) | [SEO_RESEARCH.md](SEO_RESEARCH.md) | [USER-GUIDE.md](USER-GUIDE.md) §12 |
| Needing every field/collection | [SCHEMA.md](SCHEMA.md) | [CONFIGURATION.md](CONFIGURATION.md) |
| Contributing to the code | [../CONTRIBUTING.md](../CONTRIBUTING.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§5 |
| Onboarding to the code | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§5 | README.md (repo root) |

## Document map

| Document | Audience | Purpose | Status |
|---|---|---|---|
| **ARCHITECTURE.md** | Engineers, operators | System context, process model, job engine deep dive, all pipelines (writer, indexer, images, research, WordPress sync), provider layer, security, known drift | Verified · research engine |
| **SCHEMA.md** | Engineers, integrators | 33-collection PocketBase reference (incl. 13 research collections), fields, enums, indexes, cascades, vector payload contract | Verified · research engine |
| **CONFIGURATION.md** | DevOps, operators | Every env var + per-project setting (incl. image generation, localization, research caps) + integration/prompt variables | Verified · research engine |
| **USER-GUIDE.md** | Product operators | Task-oriented walkthrough of the UI with every on-screen label in **bold** (incl. **Images** tab, images pane, **Research** workflow) | Verified · research engine |
| **OPERATIONS.md** | DevOps, SRE | Deployment, runbook, health, scaling, backups/DR, upgrades | Verified |
| **TROUBLESHOOTING.md** | Operators, on-call | Symptom → verify → resolution per area (incl. images, research) | Verified · research engine |
| **FAILURES.md** | Engineers, on-call | Per-dependency failure matrix + crash-simulation guarantees (incl. images, Google Ads, SERP) | Verified · research engine |
| **SEO_RESEARCH.md** | Engineers, SEO operators, DevOps | Google Ads OAuth + keyword research, WordPress mirror/sync, optional SERP, clustering, opportunity engine, cost/retention | Verified · research engine |

## Conventions used across the set

- **Terminology is consistent**: project, topic, article, section, job, integration,
  prompt, index run, publishing run, schedule, worker, provider.
- **UI labels are quoted verbatim** from the interface (e.g. **Write article**, **Approve**).
- **Verified vs inferred**: claims come from code/schema/tests/config. Where the docs
  and code disagree, the code wins and the discrepancy is noted rather than hidden.
- **camelCase field names** are intentional (PocketBase clients run with
  `auto_snake_case=False`).

## Source-of-truth references

- Schema: `app/scripts/bootstrap_pb.py` (idempotent bootstrap) — also mirrored by
  `pb_collections_import.json`.
- Behavior: `app/jobs/`, `app/services/`, `app/providers/`, `app/api/`.
- Run tests locally: `make test` (fakes only, no live services).
- This set supersedes the older ARCHITECTURE/SCHEMA revisions that described a
  `claimed` job status and other superseded details.
