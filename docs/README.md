# EzDistro Platform — Documentation

This folder is the documentation set for the EzDistro SEO-automation platform
(WordPress indexer + LLM article writer). It is **tracked** in the repository, so
these files ship with the codebase and are copied into the Docker image
(`COPY docs ./docs` in the Dockerfile).

```text
All documents in this folder were re-verified against the working tree on
2026-10-25 (v1.27.3). Each file carries its own "Documentation status" block;
docs/BILLING.md states its verification in the introduction.
The whole set is also served in-app at /help/ (auth-gated) — the Find
opportunities tab's "How this works" link points at /help/SEO_RESEARCH.md.
```

## Which document should I read?

| I am… | Start here | Then |
|---|---|---|
| A developer integrating / extending the platform | [ARCHITECTURE.md](ARCHITECTURE.md) | [SCHEMA.md](SCHEMA.md) · [CONFIGURATION.md](CONFIGURATION.md) |
| An operator running the service | [OPERATIONS.md](OPERATIONS.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| A content/SEO operator using the UI | [USER-GUIDE.md](USER-GUIDE.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Debugging a failed job | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | [FAILURES.md](FAILURES.md) |
| Wanting the failure-mode matrix | [FAILURES.md](FAILURES.md) | — |
| Building/using SEO research (autocomplete harvest, Google Ads, competitors, opportunities) | [SEO_RESEARCH.md](SEO_RESEARCH.md) | [USER-GUIDE.md](USER-GUIDE.md) filter + research sections |
| Selling or metering managed AI (optional) | [BILLING.md](BILLING.md) | [CONFIGURATION.md](CONFIGURATION.md) |
| Needing every field/collection | [SCHEMA.md](SCHEMA.md) | [CONFIGURATION.md](CONFIGURATION.md) |
| Contributing to the code | [../CONTRIBUTING.md](../CONTRIBUTING.md) | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§5 |
| Onboarding to the code | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§5 | [../README.md](../README.md) |

## Document map

| Document | Audience | Purpose | Status |
|---|---|---|---|
| **ARCHITECTURE.md** | Engineers, operators | System context, process model, job engine deep dive, all pipelines (writer, indexer, images, internal link building, research, landing-page analysis, WordPress sync, optional billing), provider layer, security, known drift | Verified · v1.27.3 |
| **SCHEMA.md** | Engineers, integrators | 39-collection PocketBase reference (incl. 13 research + 6 billing collections), fields, enums, indexes, cascades, vector payload contract | Verified · v1.27.3 |
| **CONFIGURATION.md** | DevOps, operators | Every env var + per-project setting (incl. image generation, localization, research caps, billing) + integration/prompt variables | Verified · v1.27.3 |
| **USER-GUIDE.md** | Product operators | Task-oriented walkthrough of the UI with every on-screen label in **bold** (nav, 15 project tabs, article workspace/review, images, research, automation, billing) | Verified · v1.27.3 |
| **OPERATIONS.md** | DevOps, SRE | Deployment (systemd, Docker), runbook, health, scaling, backups/DR, upgrades | Verified · v1.27.3 |
| **TROUBLESHOOTING.md** | Operators, on-call | Symptom → verify → resolution per area (incl. images, research, billing) | Verified · v1.27.3 |
| **FAILURES.md** | Engineers, on-call | Per-dependency failure matrix + crash-simulation guarantees (incl. images, Google Ads, SERP, payments) | Verified · v1.27.3 |
| **SEO_RESEARCH.md** | Engineers, SEO operators, DevOps | Autocomplete harvest, Google Ads OAuth + keyword research, import, WordPress mirror/sync, optional SERP, clustering, opportunity engine, cost/retention | Verified · v1.27.3 |
| **BILLING.md** | Operators, product owners | Optional PayRam billing, prepaid plans, managed-AI credits, reconciliation runbook | Verified · v1.27.3 |
| **[analytics/umami-measurement-plan.md](analytics/umami-measurement-plan.md)** | Product owners, engineers | Umami setup, event dictionary, conversion funnels, validation checklist, decision guide | Verified · v1.27.3 |

## Conventions used across the set

- **Terminology is consistent**: project (called a *website* in the UI), topic,
  article, section, job, integration, prompt, index run, publishing run, schedule,
  worker, provider, opportunity, cluster.
- **UI labels are quoted verbatim** from the interface (e.g. **Write**, **Publish
  to WordPress**), including the marketing-facing tab names introduced in v1.26.
- **Verified vs inferred**: claims come from code/schema/tests/config. Where the docs
  and code disagree, the code wins and the discrepancy is noted rather than hidden.
- **camelCase field names** are intentional (PocketBase clients run with
  `auto_snake_case=False`).
- **Optional means optional**: billing, Google Ads, SERP and managed AI are off or
  absent by default; every one of them degrades without blocking the core loop.

## Source-of-truth references

- Schema: `app/scripts/bootstrap_pb.py` (idempotent bootstrap) — also mirrored by
  `pb_collections_import.json`.
- Behavior: `app/jobs/`, `app/services/`, `app/providers/`, `app/api/`.
- Run tests locally: `make test` (fakes only, no live services).
- This set supersedes the older ARCHITECTURE/SCHEMA revisions that described a
  `claimed` job status and other superseded details.
