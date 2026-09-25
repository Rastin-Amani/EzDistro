# EzDistro Platform — Documentation

This folder is the documentation set for the EzDistro SEO-automation platform
(WordPress indexer + LLM article writer). It is **tracked** in the repository, so
these files ship with the codebase and are copied into the Docker image
(`COPY docs ./docs` in the Dockerfile).

```text
All documents verified against the working tree (post-v1.3.0, images pipeline),
2026-09-12. Each file carries its own "Documentation status" block.
Conflict findings discovered during earlier audits are recorded honestly in
ARCHITECTURE §11, SCHEMA §5, and TROUBLESHOOTING §8.
```

## Which document should I read?

| I am… | Start here | Then |
|---|---|---|
| A developer integrating / extending the platform | [ARCHITECTURE.md](ARCHITECTURE.md) | [SCHEMA.md](SCHEMA.md) · [CONFIGURATION.md](CONFIGURATION.md) |
| An operator running the service | [OPERATIONS.md](OPERATIONS.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| A content/SEO operator using the UI | [USER-GUIDE.md](USER-GUIDE.md) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Debugging a failed job | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | [FAILURES.md](FAILURES.md) |
| Wanting the failure-mode matrix | [FAILURES.md](FAILURES.md) | — |
| Needing every field/collection | [SCHEMA.md](SCHEMA.md) | [CONFIGURATION.md](CONFIGURATION.md) |
| Onboarding to the code | [ARCHITECTURE.md](ARCHITECTURE.md) §1–§5 | README.md (repo root) |

## Document map

| Document | Audience | Purpose | Status |
|---|---|---|---|
| **ARCHITECTURE.md** | Engineers, operators | System context, process model, job engine deep dive, all pipelines (writer, indexer, images), provider layer, security, known drift | Verified · covers v1.3.0 images pipeline |
| **SCHEMA.md** | Engineers, integrators | 20-collection PocketBase reference, fields, enums, indexes, cascades, vector payload contract | Verified · covers v1.3.0 images pipeline |
| **CONFIGURATION.md** | DevOps, operators | Every env var + per-project setting (incl. image generation) + integration/prompt variables | Verified · covers v1.3.0 images pipeline |
| **USER-GUIDE.md** | Product operators | Task-oriented walkthrough of the Persian UI with English translations of on-screen labels (incl. **Images** tab + images pane) | Verified · covers v1.3.0 images pipeline |
| **OPERATIONS.md** | DevOps, SRE | Deployment, runbook, health, scaling, backups/DR, upgrades | Verified |
| **TROUBLESHOOTING.md** | Operators, on-call | Symptom → verify → resolution per area (incl. images) | Verified · covers v1.3.0 images pipeline |
| **FAILURES.md** | Engineers, on-call | Per-dependency failure matrix + crash-simulation guarantees (incl. images) | Verified · refreshed |

## Conventions used across the set

- **Terminology is consistent**: project, topic, article, section, job, integration,
  prompt, index run, publishing run, schedule, worker, provider.
- **UI labels use English translations** of the Persian UI labels (e.g. **Write article**, **Approve**); the UI itself remains Persian RTL.
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
